#!/usr/bin/env python

from dateutil.tz import tzutc
import atexit
import datetime
import os
import pytz
import signal
import sys
import yaml
import os.path
import time
from copy import deepcopy
from sys import path as python_path
import crits
import edge

# shamelessly plundered from repository.edge.tools :-P
#
# recursive getattr()
# syntax is the same as getattr, but the requested
# attribute is a list instead of a string.
#
# e.g. confidence = rgetattr(apiobject,['confidence','value','value'])
#                 = apiobject.confidence.value.value
#
def rgetattr(object_ ,list_ ,default_=None):
    """recursive getattr using a list"""
    if object_ is None:
        return default_
    if len(list_) == 1:
        return getattr(object_, list_[0], default_)
    else:
        return rgetattr(getattr(object_, list_[0], None), list_[1:], default_)

    
def nowutcmin():
    """time now, but only minute-precision"""
    return datetime.datetime.utcnow().replace(second=0,microsecond=0).replace(tzinfo=pytz.utc)


def epoch_start():
    '''it was the best of times, it was the worst of times...'''
    return datetime.datetime.utcfromtimestamp(0).replace(tzinfo=pytz.utc)


def parse_config(file_):
    '''parse a yaml config file'''
    try:
        return(yaml.safe_load(file(file_, 'r')))
    except yaml.YAMLError:
        print('error parsing yaml file: %s; check your syntax!' % file_)
        exit()


def signal_handler(num, frame):
    '''signal handler function for Daemon'''
    sys.exit()


class Daemon:
    '''generic daemon class'''
    def __init__(self, config, stdin='/dev/null',
                 stdout='/dev/null', stderr='/dev/null'):
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.config = config
        self.working_dir = self.config['daemon']['working_dir']
        self.pidfile = os.path.join(self.working_dir, config['daemon']['pid'])
        self.logger = self.config['logger']


    def daemonize(self):
        '''do the UNIX double-fork magic, see Stevens' "Advanced
        Programming in the UNIX Environment" for details (ISBN
        0201563177)
        http://www.erlenstar.demon.co.uk/unix/faq_2.html#SEC16'''
        try:
            pid = os.fork()
            if pid > 0:
                # exit first parent
                sys.exit(0)
        except OSError as e:
            self.logger.error("fork #1 failed: %d (%s)\n" % (e.errno, e.strerror))
            self.logger.exception(e)
            sys.exit(1)

        # decouple from parent environment
        os.chdir(self.working_dir)
        os.setsid()
        os.umask(0)

        # do second fork
        try:
            pid = os.fork()
            if pid > 0:
                # exit from second parent
                sys.exit(0)
        except OSError as e:
            self.logger.error("fork #2 failed: %d (%s)\n" % (e.errno, e.strerror))
            self.logger.exception(e)
            sys.exit(1)

        # redirect standard file descriptors

        if not self.config['daemon']['debug']:
            sys.stdout.flush()
            sys.stderr.flush()
            si = file(self.stdin, 'r')
            so = file(self.stdout, 'a+')
            se = file(self.stderr, 'a+', 0)
            os.dup2(si.fileno(), sys.stdin.fileno())
            os.dup2(so.fileno(), sys.stdout.fileno())
            os.dup2(se.fileno(), sys.stderr.fileno())

        atexit.register(self.cleanup_and_die)
        # write pidfile
        pid = str(os.getpid())
        try:
            file(self.pidfile, 'w+').write("%s\n" % pid)
        except Exception as e:
            self.logger.error('could not write to pidfile %s' % self.pidfile)
            self.logger.exception(e)

    def cleanup_and_die(self):
        '''cleanup function'''
        self.logger.info('SIGINT received! Cleaning up and killing processes...')
        try:
            os.remove(self.pidfile)
            yaml_ = deepcopy(self.config)
            del yaml_['config_file']
            del yaml_['logger']
            file_ = file(self.config['config_file'], 'w')
            yaml.dump(yaml_, file_, default_flow_style=False)
            file_.close()
        except Exception as e:
            self.logger.error('could not delete pidfile %s' % self.pidfile)
            self.logger.exception(e)


    def start(self):
        '''Start the daemon'''
        # Check for a pidfile to see if the daemon already runs
        pid = None
        try:
            if os.path.isfile(self.pidfile):
                pf = file(self.pidfile, 'r')
                pid = int(pf.read().strip())
                pf.close()
        except IOError as e:
            self.logger.error('could not access pidfile %s' % self.pidfile)
            self.logger.exception(e)

        if pid:
            self.logger.error(
                'pidfile %s already exists. Daemon already running?' %
                self.pidfile)
            sys.exit(1)

        # Start the daemon
        self.daemonize()
        self.run()

    def stop(self):
        '''Stop the daemon'''
        # Get the pid from the pidfile
        try:
            pf = file(self.pidfile, 'r')
            pid = int(pf.read().strip())
            pf.close()
        except IOError as e:
            pid = None
            self.logger.error('could not access pidfile %s' % self.pidfile)
            self.logger.exception(e)


        if not pid:
            self.logger.error(
                'pidfile %s does not exist. Daemon not running?' %
                self.pidfile)
            return  # not an error in a restart

        # Try killing the daemon process
        try:
            while True:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.1)
        except OSError as e:
            # process is already dead
            if str(e).find("No such process") > 0:
                if os.path.exists(self.pidfile):
                    os.remove(self.pidfile)
            else:
                self.logger.error('something went wrong while trying to kill process %i' % pid)
                self.logger.exception(e)
                sys.exit(1)

    def restart(self):
        '''Restart the daemon'''
        self.stop()
        self.start()

    def run(self):
        '''daemon main logic'''
        while True:
            enabled_crits_sites = list()
            enabled_edge_sites = list()
            for crits_site in self.config['crits']['sites'].keys():
                if self.config['crits']['sites'][crits_site]['enabled']: enabled_crits_sites.append(crits_site)
            for edge_site in self.config['edge']['sites'].keys():
                if self.config['edge']['sites'][edge_site]['enabled']: enabled_edge_sites.append(edge_site)
            for crits_site in enabled_crits_sites:
                for edge_site in enabled_edge_sites:
                    # check if (and when) we synced source and destination...
                    state_key = crits_site + '_to_' + edge_site
                    now = nowutcmin()
                    last_run = None
                    if not isinstance(self.config['state'], dict):
                        self.config['state'] = dict()
                    if not state_key in self.config['state'].keys():
                        self.config['state'][state_key] = dict()
                    if not 'crits_to_edge' in self.config['state'][state_key].keys():
                        self.config['state'][state_key]['crits_to_edge'] = dict()
                    if 'timestamp' in self.config['state'][state_key]['crits_to_edge'].keys():
                        last_run = self.config['state'][state_key]['crits_to_edge']['timestamp'].replace(tzinfo=pytz.utc)
                    else: last_run = epoch_start()
                    if now >= last_run + datetime.timedelta(seconds=self.config['crits']['sites'][crits_site]['api']['poll_interval']):
                        self.logger.info('initiating crits=>edge sync between %s and %s' % (crits_site, edge_site))
                        crits.crits2edge(self.config, crits_site, edge_site, daemon=True)
                    else: continue
            for edge_site in enabled_edge_sites:
                for crits_site in enabled_crits_sites:
                    # check if (and when) we synced source and destination...
                    state_key = edge_site + '_to_' + crits_site
                    now = nowutcmin()
                    last_run = None
                    if not isinstance(self.config['state'], dict):
                        self.config['state'] = dict()
                    if not state_key in self.config['state'].keys():
                        self.config['state'][state_key] = dict()
                    if not 'edge_to_crits' in self.config['state'][state_key].keys():
                        self.config['state'][state_key]['edge_to_crits'] = dict()
                    if 'timestamp' in self.config['state'][state_key]['edge_to_crits'].keys():
                        last_run = self.config['state'][state_key]['edge_to_crits']['timestamp'].replace(tzinfo=pytz.utc)
                    else: last_run = epoch_start()
                    if now >= last_run + datetime.timedelta(seconds=self.config['edge']['sites'][edge_site]['taxii']['poll_interval']):
                        self.logger.info('initiating edge=>crits sync between %s and %s' % (edge_site, crits_site))
                        edge.edge2crits(self.config, edge_site, crits_site, daemon=True)
                    else: continue
            time.sleep(1)

    