import socket
import urllib
import time
import subprocess
import platform
import psutil
import os
import json
import select
import requests
import logging
from zipfile import ZipFile
from zipfile import BadZipfile
import gocept.cache.method
from threading import Thread
from threading import Lock
import Queue # for windows

from flask import redirect
from flask import url_for
from flask import request
from flask import jsonify
from flask import Blueprint
from uuid import getnode as get_mac_address

from application import app
from requests.exceptions import ConnectionError

MAC_ADDRESS = get_mac_address()  # the MAC address of the worker
HOSTNAME = socket.gethostname()  # the hostname of the worker
SYSTEM = platform.system() + ' ' + platform.release()
PROCESS = None
LOCK = Lock()
ACTIVITY = None
LOG = None
TIME_INIT = None
CONNECTIVITY = False

if platform.system() is not 'Windows':
    from fcntl import fcntl, F_GETFL, F_SETFL
    from signal import SIGKILL
else:
    from signal import CTRL_C_EVENT


BRENDER_MANAGER = app.config['BRENDER_MANAGER']

controller_bp = Blueprint('controllers', __name__)

def http_request(command, values):
    global CONNECTIVITY
    params = urllib.urlencode(values)
    try:
        urllib.urlopen('http://' + BRENDER_MANAGER + '/' + command, params)
        #print(f.read())
    except IOError:
        CONNECTIVITY = False
        logging.error("Could not connect to manager to register")

def register_worker(port):
    """This is going to be an HTTP request to the server with all the info
    for registering the render node.
    """
    global CONNECTIVITY

    while True:
        try:
            manager_url = "http://{0}/info".format(app.config['BRENDER_MANAGER'])
            requests.get(manager_url)
            CONNECTIVITY = True
            break
        except ConnectionError:
            logging.error("Could not connect to manager to register")
            CONNECTIVITY = False
            pass
        time.sleep(1)

    http_request('workers', {'port': port,
                               'hostname': HOSTNAME,
                               'system': SYSTEM})

def _checkProcessOutput(process):
    ready = select.select([process.stdout.fileno(),
                           process.stderr.fileno()],
                          [], [])
    full_buffer = ''
    for fd in ready[0]:
        while True:
            try:
                buffer = os.read(fd, 1024)
                if not buffer:
                    break
                print buffer
            except OSError:
                break
            full_buffer += buffer
    return full_buffer

def _checkOutputThreadWin(fd, q):
    while True:
        buffer = os.read(fd, 1024)
        if not buffer:
            break
        else:
            print buffer
            q.put(buffer)

def _checkProcessOutputWin(process, q):
    full_buffer = ''
    while True:
        try:
            buffer = q.get_nowait()
            if not buffer:
                break
        except:
            break
        full_buffer += buffer
    return full_buffer

def send_thumbnail(manager_url, file_path, params):
    try:
        thumbnail_file = open(file_path, 'r')
    except IOError, e:
        logging.error('Cant open thumbnail: {0}'.format(e))
        return
    try:
        requests.post(manager_url, files={'file': thumbnail_file}, data=params)
    except ConnectionError:
        logging.error("Can't send Thumbnail to manager: {0}".format(manager_url))
    thumbnail_file.close()

def _parse_output(tmp_buffer, options):
    global ACTIVITY
    global LOG

    task_id = options['task_id']
    module_name = 'application.task_parsers.{0}'.format(options['task_parser'])
    task_parser = None
    try:
        module_loader = __import__(module_name, globals(), locals(), ['task_parser'], 0)
        task_parser = module_loader.task_parser
    except ImportError, e:
        print('Cant find module {0}: {1}'.format(module_name, e))

    if not LOG:
        LOG=""

    if task_parser:
        parser_output = task_parser.parse(tmp_buffer,options, ACTIVITY)
        if parser_output:
            ACTIVITY = parser_output
            activity=json.loads(parser_output)

        if activity.get('thumbnail'):
            params = dict(task_id=task_id)
            manager_url = "http://%s/tasks/thumbnails" % (app.config['BRENDER_MANAGER'])
            request_thread = Thread(target=send_thumbnail, args=(manager_url, activity.get('thumbnail'), params))
            request_thread.start()

        if activity.get('path_not_found'):
            # kill process
            # update()
            pass


    LOG = "{0}{1}".format(LOG, tmp_buffer)
    logpath = os.path.join(app.config['TMP_FOLDER'], "{0}.log".format(task_id))
    f = open(logpath,"a")
    f.write(tmp_buffer)
    f.close()

def _interactiveReadProcessWin(process, options):
    full_buffer = ''
    tmp_buffer = ''
    q = Queue.Queue()
    t_out = Thread(target=_checkOutputThreadWin, args=(process.stdout.fileno(), q,))
    t_err = Thread(target=_checkOutputThreadWin, args=(process.stderr.fileno(), q,))

    t_out.start()
    t_err.start()

    while True:
        tmp_buffer = _checkProcessOutputWin(process, q)
        if tmp_buffer:
            _parse_output(tmp_buffer, options)
            pass
        # full_buffer += tmp_buffer
        if process.poll() is not None:
            break

    t_out.join()
    t_err.join()
    # full_buffer += _checkProcessOutputWin(process, q)
    return (process.returncode, full_buffer)

def _interactiveReadProcess(process, options):
    full_buffer = ''
    tmp_buffer = ''
    while True:
        tmp_buffer = _checkProcessOutput(process)

        if tmp_buffer:
            _parse_output(tmp_buffer, options)
            pass
        if process.poll() is not None:
            break
    # It might be some data hanging around in the buffers after
    # the process finished
    # full_buffer += _checkProcessOutput(process)

    return (process.returncode, full_buffer)

@app.route('/')
def index():
    return redirect(url_for('info'))

@app.route('/info')
def info():
    global PROCESS
    global ACTIVITY
    global LOG
    global TIME_INIT
    global CONNECTIVITY

    if PROCESS:
        status = 'rendering'
    else:
        status = 'enabled'
    if not CONNECTIVITY:
        status = 'error'

    time_cost=None
    if TIME_INIT:
        time_cost=int(time.time())-TIME_INIT

    return jsonify(mac_address=MAC_ADDRESS,
                   hostname=HOSTNAME,
                   system=SYSTEM,
                   activity=ACTIVITY,
                   log=LOG,
                   time_cost=time_cost,
                   status=status)

def clear_dir(cleardir):
    if os.path.exists(cleardir):
        for root, dirs, files in os.walk(cleardir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))



def run_blender_in_thread(options):
    """We take the command and run it
    """
    global PROCESS
    global ACTIVITY
    global LOG
    global TIME_INIT
    global CONNECTIVITY

    render_command = json.loads(options['task_command'])

    workerstorage = app.config['TMP_FOLDER']
    tmppath = os.path.join(
        workerstorage, str(options['job_id']))
    outpath = os.path.join(tmppath, 'output')
    clear_dir(outpath)
    jobpath = os.path.join(tmppath, str(options['job_id']), "")

    outputpath = os.path.join(
        workerstorage,
        str(options['job_id']),
        'output',
        str(options['task_id'])
    )

    dependpath = os.path.join(
        workerstorage,
        str(options['job_id']),
        'depend',
    )


    for cmd, val in enumerate(render_command):
        render_command[cmd] = render_command[cmd].replace(
            "==jobpath==",jobpath)
        render_command[cmd] = render_command[cmd].replace(
            "==outputpath==",outputpath)

    os.environ['WORKER_DEPENDPATH'] = dependpath
    os.environ['WORKER_OUTPUTPATH'] = outputpath
    os.environ['WORKER_JOBPATH'] = jobpath

    print ( "Running %s" % render_command)

    TIME_INIT = int(time.time())
    PROCESS = subprocess.Popen(render_command,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)

    # Make I/O non blocking for unix
    if platform.system() is not 'Windows':
        flags = fcntl(PROCESS.stdout, F_GETFL)
        fcntl(PROCESS.stdout, F_SETFL, flags | os.O_NONBLOCK)
        flags = fcntl(PROCESS.stderr, F_GETFL)
        fcntl(PROCESS.stderr, F_SETFL, flags | os.O_NONBLOCK)

    #flask.g.blender_process = process
    (retcode, full_output) =  _interactiveReadProcess(PROCESS, options) \
        if (platform.system() is not "Windows") \
        else _interactiveReadProcessWin(PROCESS, options)

    log = LOG
    activity = ACTIVITY
    time_init = TIME_INIT


    #flask.g.blender_process = None
    #print(full_output)
    script_dir = os.path.dirname(__file__)
    rel_path = 'render_log_' + HOSTNAME + '.log'
    abs_file_path = os.path.join(script_dir, rel_path)
    with open(abs_file_path, 'w') as f:
        f.write(full_output)

    if retcode == -9:
        status='aborted'
    elif retcode != 0:
        status='failed'
    else:
        status='finished'

    logging.debug(status)

    if time_init:
        time_cost=int(time.time())-time_init
    else:
        time_cost = 0
        logging.error("time_init is None")

    workerstorage = app.config['TMP_FOLDER']
    taskpath = os.path.join(
        workerstorage,
        str(options['job_id']),
    )
    taskfile = os.path.join(
        taskpath,
        'taskfileout_{0}_{1}.zip'.format(options['job_id'], options['task_id'])
    )
    zippath = os.path.join(
        taskpath,
        'output',
        str(options['task_id']),
    )

    with ZipFile(taskfile, 'w') as taskzip:
        f = []
        for dirpath, dirnames, filenames in os.walk(zippath):
            for fname in filenames:
                filepath = os.path.join(dirpath, fname)
                taskzip.write(filepath, fname)

    tfiles = [
        ('taskfile', (
            'taskfile.zip', open(taskfile, 'rb'), 'application/zip'))]

    params = {
        'status': status,
        'log': log,
        'activity': activity,
        'time_cost': time_cost,
        'job_id': options['job_id'],
        }

    try:
        requests.patch(
            'http://'+BRENDER_MANAGER+'/tasks/'+options['task_id'],
            data=params,
            files=tfiles,
        )
        CONNECTIVITY = True
    except ConnectionError:
        logging.error(
            'Cant connect with the Manager {0}'.format(BRENDER_MANAGER))
        CONNECTIVITY = False

    logging.debug( 'Return code: {0}'.format(retcode) )

    PROCESS = None
    ACTIVITY = None
    LOG = None
    TIME_INIT = None


def extract_file(filename, taskpath, zippath, zipname):
    if not filename in request.files:
        return

    if request.files[filename]:
        try:
            os.mkdir(taskpath)
        except:
            logging.error("Error creatig folder:{0}".format(taskpath))
        taskfile = os.path.join(
            taskpath, zipname)
        request.files[filename].save( taskfile )

        if not os.path.exists(zippath):
            try:
                os.mkdir(zippath)
            except:
                logging.error("Error creatig folder:{0}".format(zippath))

        try:
            with ZipFile(taskfile, 'r') as jobzip:
                    jobzip.extractall(path=zippath)
        except BadZipfile, e:
            logging.error("File is not zip {0}".format(e))


@app.route('/execute_task', methods=['POST'])
def execute_task():
    global PROCESS
    global LOCK

    if PROCESS:
        return '{error:Processus failed}', 500

    options = {
        'task_id': request.form['task_id'],
        'job_id': request.form['job_id'],
        'task_parser': request.form['task_parser'],
        'settings': request.form['settings'],
        'task_command': request.form['task_command'],
    }

    workerstorage = app.config['TMP_FOLDER']
    taskpath = os.path.join(workerstorage, str(options['job_id']))
    zippath = os.path.join(taskpath, str(options['job_id']))

    extract_file (
        'jobfile',
        taskpath,
        zippath,
        'taskfile_{0}.zip'.format(options['job_id'])
    )

    extract_file (
        'jobsupportfile',
        taskpath,
        zippath,
        'tasksupportfile_{0}.zip'.format(options['job_id'])
    )

    deppath = os.path.join(taskpath, 'depend')
    clear_dir(deppath)
    extract_file (
        'jobdepfile',
        taskpath,
        deppath,
        'taskdepfile_{0}.zip'.format(options['job_id'])
    )

    options['jobpath'] = taskpath

    LOCK.acquire()
    PROCESS = None
    LOG = None
    TIME_INIT = None
    ACTIVITY = None

    render_thread = Thread(target=run_blender_in_thread, args=(options,))
    render_thread.start()
    LOCK.release()
    return jsonify(dict(pid=0))

@app.route('/pid')
def get_pid():
    global PROCESS
    response = dict(pid=PROCESS.pid)
    return jsonify(response)

@app.route('/command', methods=['HEAD'])
def get_command():
    # TODO Return the running command
    return '', 503

@app.route('/kill', methods=['DELETE'])
def update():
    global PROCESS
    global LOCK
    if not PROCESS:
        return '',204

    logging.info('killing {0}'.format(PROCESS.pid))
    if platform.system() is 'Windows':
        os.kill(PROCESS.pid, CTRL_C_EVENT)
    else:
        os.kill(PROCESS.pid, SIGKILL)

    LOCK.acquire()
    PROCESS = None
    LOCK.release()
    return '', 204

def online_stats(system_stat):
    '''
    if 'blender_cpu' in [system_stat]:
        try:
            find_blender_process = [x for x in psutil.process_iter() if x.name == 'blender']
            cpu = []
            if find_blender_process:
                for process in find_blender_process:
                    cpu.append(process.get_cpu_percent())
                    return round(sum(cpu), 2)
            else:
                return int(0)
        except psutil._error.NoSuchProcess:
            return int(0)
    if 'blender_mem' in [system_stat]:
        try:
            find_blender_process = [x for x in psutil.get_process_list() if x.name == 'blender']
            mem = []
            if find_blender_process:
                for process in find_blender_process:
                    mem.append(process.get_memory_percent())
                    return round(sum(mem), 2)
            else:
                return int(0)
        except psutil._error.NoSuchProcess:
            return int(0)
    '''

    if 'system_cpu' in [system_stat]:
        try:
            cputimes = psutil.cpu_percent(interval=1)
            return cputimes
        except:
            return int(0)
    if 'system_mem' in [system_stat]:
        mem_percent = psutil.phymem_usage().percent
        return mem_percent
    if 'system_disk' in [system_stat]:
        disk_percent = psutil.disk_usage('/').percent
        return disk_percent

def offline_stats(offline_stat):
    if 'number_cpu' in [offline_stat]:
        return psutil.NUM_CPUS

    if 'arch' in [offline_stat]:
        return platform.machine()

@gocept.cache.method.Memoize(5)
def get_system_load_frequent():
    if platform.system() is not "Windows":
        load = os.getloadavg()
        return ({
            "load_average": ({
                "1min": round(load[0], 2),
                "5min": round(load[1], 2),
                "15min": round(load[2], 2)
                }),
            "worker_cpu_percent": online_stats('system_cpu'),
            #'worker_blender_cpu_usage': online_stats('blender_cpu')
            })
    else:
        # os.getloadavg does not exists on Windows
        return ({
            "load_average":({
                "1min": '?',
                "5min": '?',
                "15min": '?'
            }),
           "worker_cpu_percent": online_stats('system_cpu')
        })

@gocept.cache.method.Memoize(120)
def get_system_load_less_frequent():
    return ({
        "worker_num_cpus": offline_stats('number_cpu'),
        "worker_architecture": offline_stats('arch'),
        "worker_mem_percent": online_stats('system_mem'),
        "worker_disk_percent": online_stats('system_disk'),
        # "worker_blender_mem_usage": online_stats('blender_mem')
        })

@app.route('/run_info')
def run_info():
    print('[Debug] get_system_load for %s') % HOSTNAME
    return jsonify(mac_address=MAC_ADDRESS,
                   hostname=HOSTNAME,
                   system=SYSTEM,
                   update_frequent=get_system_load_frequent(),
                   update_less_frequent=get_system_load_less_frequent()
                   )


@app.route('/tasks/file/<int:job_id>')
def check_file(job_id):
    """Check if the Workers already have the file
    """
    workerstorage = app.config['TMP_FOLDER']
    jobpath = os.path.join(workerstorage, str(job_id))
    filepath = os.path.join(jobpath, "jobfile_{0}.zip".format(job_id))
    r = json.dumps({'file': os.path.isfile(filepath)})
    return r, 200
