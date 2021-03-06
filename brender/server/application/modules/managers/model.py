from application import db
from urllib import urlopen
from sqlalchemy import UniqueConstraint

import requests
import logging
from requests.exceptions import Timeout
from requests.exceptions import ConnectionError

class Manager(db.Model):
    """Model for the managers connected to the server. When a manager
    connects, we verify that it has connected before, by checking its
    ip_address and port fields (which are unique keys).

    This will be updated to support a UUID, which will be stored in the
    manager's setting, as well as in the uuid field of the model.
    """
    id = db.Column(db.Integer, primary_key=True)
    uuid = db.Column(db.String(128), nullable=True, unique=True)
    ip_address = db.Column(db.String(15))
    port = db.Column(db.Integer)
    name = db.Column(db.String(50), nullable=True)

    has_virtual_workers = db.Column(db.SmallInteger(), default=0)

    __table_args__ = (UniqueConstraint('ip_address', 'port', name='connection_uix'),)

    @property
    def host(self):
        return self.ip_address + ':' + str(self.port)

    def is_available(self):
        #return self.has_virtual_workers == 1 or self.total_workers - self.running_tasks > 0
        try:
            r = requests.get("http://" + self.host + '/workers')
            info = r.json()
            for worker_hostname in info:
                if not info[worker_hostname]['current_task'] and info[worker_hostname]['connection']=='online' and info[worker_hostname]['status']=='enabled':
                    return True
        except Timeout:
            logging.warning("Manager {0} is offline".format(self.host))
            return False
        except ConnectionError:
            logging.warning("Manager {0} is offline".format(self.host))
            return False

    @property
    def is_connected(self):
        try:
            urlopen("http://" + self.host)
            return True
        except:
            logging.warning("Manager %s is offline" % self.name)
            return False


    def __repr__(self):
        return '<Manager %r>' % self.id
