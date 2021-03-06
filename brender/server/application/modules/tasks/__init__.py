import os
import logging
import json
from PIL import Image
from platform import system
from threading import Thread
from sqlalchemy import func
from zipfile import ZipFile

from flask import abort
from flask import jsonify
from flask import render_template
from flask import request
from flask import send_from_directory
from flask.ext.restful import Resource
from flask.ext.restful import reqparse

from application import app
from application import db

from application.utils import http_rest_request
from application.utils import get_file_ext

from application.modules.tasks.model import Task
from application.modules.managers.model import Manager
from application.modules.jobs.model import Job
from application.modules.projects.model import Project
from application.modules.settings.model import Setting
from application.modules.jobs.model import JobManagers

from requests.exceptions import ConnectionError


from werkzeug.datastructures import FileStorage

parser = reqparse.RequestParser()
parser.add_argument('id', type=int)
parser.add_argument('status', type=str)
parser.add_argument('log', type=str)
parser.add_argument('time_cost', type=int)
parser.add_argument('activity', type=str)
parser.add_argument('taskfile', type=FileStorage, location='files')


class TaskApi(Resource):
    @staticmethod
    def create_task(job_id, task_type, task_settings, name, child_id, parser):
        # TODO attribution of the best manager
        task = Task(job_id=job_id,
            name=name,
            type=task_type,
            settings=json.dumps(task_settings),
            status='ready',
            priority=50,
            log=None,
            time_cost=None,
            activity=None,
            child_id=child_id,
            parser=parser,
            )
        db.session.add(task)
        db.session.commit()
        return task.id

    @staticmethod
    def create_tasks(job):
        """Send the job to the Job Compiler"""
        #from application.job_compilers.simple_blender_render import job_compiler
        module_name = 'application.job_compilers.{0}'.format(job.type)
        job_compiler = None
        try:
            module_loader = __import__(module_name, globals(), locals(), ['job_compiler'], 0)
            job_compiler = module_loader.job_compiler
        except ImportError, e:
            print('Cant find module {0}: {1}'.format(module_name, e))
            return

        project = Project.query.filter_by(id = job.project_id).first()
        job_compiler.compile(job, project, TaskApi.create_task)

    @staticmethod
    def start_task(manager, task):
        """Execute a single task
        We pass manager and task as objects (and at the moment we use a bad
        way to get the additional job information - should be done with join)
        """

        params={'priority':task.priority,
            'type':task.type,
            'parser':task.parser,
            'task_id':task.id,
            'job_id':task.job_id,
            'settings':task.settings}


        task.status = 'running'
        task.manager_id = manager.id
        db.session.add(task)
        db.session.commit()

        r = http_rest_request(manager.host, '/tasks/file/{0}'.format(task.job_id), 'get')
        # print ('testing file')
        # print (r)
        if not r['file']:
            job = Job.query.get(task.job_id)
            serverstorage = app.config['SERVER_STORAGE']
            projectpath = os.path.join(serverstorage, str(job.project_id))
            jobpath = os.path.join(projectpath, str(job.id))
            zippath = os.path.join(jobpath, "jobfile_{0}.zip".format(job.id))
            try:
                jobfile = [('jobfile', ('jobfile.zip', open(zippath, 'rb'), 'application/zip'))]
            except IOError, e:
                logging.error(e)
            try:
                # requests.post(serverurl, files = render_file , data = job_properties)
                r = http_rest_request(manager.host, '/tasks', 'post', params, files=jobfile)
            except ConnectionError:
                print ("Connection Error: {0}".format(serverurl))

        else:
            r = http_rest_request(manager.host, '/tasks', 'post', params)

        if not u'status' in r:
            task.status = 'running'
            task.manager_id = manager.id
            db.session.add(task)
            db.session.commit()
        # TODO  get a reply from the worker (running, error, etc)

    @staticmethod
    def dispatch_tasks():
        """
        We want to assign a task according to its priority and its assignability
        to the less requested available compatible limited manager. If it does not exist,
        we assign it to the unlimited compatible manager. Otherwise, keep the task and wait
        to the next call to dispatch_tasks


        The task dispaching algorithm works as follows:

        - collect all asked managers
            - detect managers with non virtual workers
        - check if we are dispatching the tasks of a specific job
            - sort tasks in order by priority and assignability to compatible managers
            - assign each task to a compatible manager
        - otherwise
            - assign each task to a compatible manager


        How to assign a task to a manager:

        - collect compatible and available managers and sort them by request
        - if manager's list is not empty
            - assign task to first manager of the list
        - else
            - assign task to compatible unlimited manager
            - if no compatible manager is unlimited, do not assign task (it will wait the next call of dispatch_tasks)
        """
        logging.info('Dispatch tasks')

        tasks = None

        managers = db.session.query(Manager, func.count(JobManagers.manager_id))\
                .join(JobManagers, Manager.id == JobManagers.manager_id)\
                .filter(Manager.has_virtual_workers == 0)\
                .group_by(Manager)\
                .all()

        #Sort task by priority and then by amount of possible manager
        tasks = db.session.query(Task, func.count(JobManagers.manager_id).label('mgr'))\
                    .join(JobManagers, Task.job_id == JobManagers.job_id)\
                    .filter(db.or_(Task.status == 'ready', Task.status == 'aborted'))\
                    .group_by(Task)\
                    .order_by(Task.priority, 'mgr')\
                    .all()

        for t in tasks:
            unfinished_parents = Task.query.filter(Task.child_id==t[0].id, Task.status!='finished').count()
            if unfinished_parents>0:
                continue

            job = Job.query.filter_by(id=t[0].job_id).first()
            if not job.status in ['running', 'ready']:
                continue

            rela = db.session.query(JobManagers.manager_id)\
                .filter(JobManagers.job_id == t[0].job_id)\
                .all()

            # Get only accepted available managers
            managers_available = filter(lambda m : m[0].is_available() and (m[0].id,) in rela, managers)

            #Sort managers_available by asking
            managers_available.sort(key=lambda m : m[1])

            if not managers_available:
                print ('No Managers available')
                #Get the first unlimited manager available
                manager_unlimited = Manager.query\
                    .join(JobManagers, Manager.id == JobManagers.manager_id)\
                    .filter(JobManagers.job_id == t[0].job_id)\
                    .filter(Manager.has_virtual_workers == 1)\
                    .first()

                if manager_unlimited:
                    TaskApi.start_task(manager_unlimited, t[0])

            else:
                print ('Managers available')
                TaskApi.start_task(managers_available[0][0], t[0])

    @staticmethod
    def delete_task(task_id):
        # At the moment this function is not used anywhere
        try:
            task = Tasks.query.get(task_id)
        except Exception, e:
            print(e)
            return 'error'
        task.delete_instance()
        print('Deleted task', task_id)

    @staticmethod
    def delete_tasks(job_id):
        tasks = Task.query.filter_by(job_id=job_id)
        for t in tasks:
            db.session.delete(t)
            db.session.commit()
        logging.info("All tasks deleted for job {0}".format(job_id))

    @staticmethod
    def stop_task(task_id):
        """Stop a single task
        """
        print ('Stoping task %s' % task_id)
        task = Task.query.get(task_id)
        manager = Manager.query.filter_by(id = task.manager_id).first()
        try:
            delete_task = http_rest_request(manager.host, '/tasks/' + str(task.id), 'delete')
        except:
            logging.info("Error deleting task from Manager")
            return
            pass
        task.status = 'ready'
        db.session.add(task)
        db.session.commit()
        print "Task %d stopped" % task_id

    @staticmethod
    def stop_tasks(job_id):
        """We stop all the tasks for a specific job
        """
        tasks = Task.query.\
            filter_by(job_id = job_id).\
            filter_by(status = 'running').\
            all()

        print tasks
        for t in tasks:
            print t
        map(lambda t : TaskApi.stop_task(t.id), tasks)
        #TaskApi.delete_tasks(job_id)

    def get(self):
        from decimal import Decimal
        tasks = {}
        percentage_done = 0
        for task in Task.query.all():

            frame_count = 1
            current_frame = 0
            percentage_done = Decimal(current_frame) / Decimal(frame_count) * Decimal(100)
            percentage_done = round(percentage_done, 1)
            tasks[task.id] = {"job_id": task.job_id,
                            "chunk_start": 0,
                            "chunk_end": 0,
                            "current_frame": 0,
                            "status": task.status,
                            "percentage_done": percentage_done,
                            "priority": task.priority}
        return jsonify(tasks)

    @staticmethod
    def generate_thumbnails(job, start, end):
        # FIXME problem with PIL (string index out of range)
        #thumb_dir = RENDER_PATH + "/" + str(job.id)
        project = Project.query.get(job.project_id)
        thumbnail_dir = os.path.join(project.render_path_server, str(job.id), 'thumbnails')
        if not os.path.exists(thumbnail_dir):
            print '[Debug] ' + os.path.abspath(thumbnail_dir) + " does not exist"
            os.makedirs(thumbnail_dir)
        for i in range(start, end + 1):
            # TODO make generic extension
            #img_name = ("0" if i < 10 else "") + str(i) + get_file_ext(job.format)
            img_name = '{0:05d}'.format(i) + get_file_ext(job.format)
            #file_path = thumb_dir + "/" + str(i) + '.thumb'
            file_path = os.path.join(thumbnail_dir, '{0:05d}'.format(i), '.thumb')
            # We can't generate thumbnail from multilayer with pillow
            if job.format != "MULTILAYER":
                if os.path.exists(file_path):
                    os.remove(file_path)
                frame = os.path.abspath(
                    os.path.join(project.render_path_server, str(job.id), img_name))
                img = Image.open(frame)
                img.thumbnail((150, 150), Image.ANTIALIAS)
                thumbnail_path = os.path.join(thumbnail_dir, '{0:05d}'.format(i) + '.thumb')
                img.save(thumbnail_path, job.format)

    def post(self):
        args = parser.parse_args()
        task_id = args['id']
        status = args['status'].lower()
        log = args['log']
        time_cost = args['time_cost']
        activity = args['activity']
        task = Task.query.get(task_id)
        if task is None:
            return '', 404

        job = Job.query.get(task.job_id)
        if job is None:
            return '', 404

        serverstorage = app.config['SERVER_STORAGE']
        projectpath = os.path.join(serverstorage, str(job.project_id))

        try:
            os.mkdir(projectpath)
        except:
            pass

        if args['taskfile']:
            jobpath = os.path.join(projectpath, str(job.id))
            try:
                os.mkdir(jobpath)
            except:
                pass
            try:
                os.mkdir(os.path.join(jobpath, 'output'))
            except:
                pass
            taskfile = os.path.join(
                    app.config['TMP_FOLDER'], 'taskfileout_{0}_{1}.zip'.format(job.id, task_id))
            args['taskfile'].save( taskfile )

            zippath = os.path.join(jobpath, 'output')

            with ZipFile(taskfile, 'r') as jobzip:
                jobzip.extractall(path=zippath)

            os.remove(taskfile)

        status_old = task.status
        task.status = status
        task.log = log
        task.time_cost = time_cost
        task.activity = activity
        db.session.add(task)
        db.session.commit()

        if status != status_old:
            job = Job.query.get(task.job_id)
            manager = Manager.query.get(task.manager_id)
            logging.info('Task {0} changed from {1} to {2}'.format(task_id, status_old, status))

            # Check if all tasks have been completed
            if all((lambda t : t.status in ['finished', 'failed'])(t) for t in Task.query.filter_by(job_id=job.id).all()):
                failed_tasks = Task.query.filter_by(job_id=job.id, status='failed').count()
                print ('[Debug] %d tasks failed before') % failed_tasks
                if failed_tasks > 0 or status == 'failed':
                    job.status = 'failed'
                else:
                    job.status = 'completed'
                db.session.add(job)
                db.session.commit()

            render_thread = Thread(target=TaskApi.dispatch_tasks())
            render_thread.start()

        return '', 204


class TaskFileOutputApi(Resource):
    def get(self, task_id):
        """Given a task_id returns the output zip file
        """
        serverstorage = app.config['SERVER_STORAGE']
        task = Task.query.get(task_id)
        job = Job.query.get(task.job_id)
        projectpath = os.path.join(serverstorage, str(job.project_id))
        jobpath = os.path.join(projectpath, str(job.id), 'output')
        return send_from_directory(jobpath, 'taskfileout_{0}_{1}.zip'.format(job.id, task_id))
