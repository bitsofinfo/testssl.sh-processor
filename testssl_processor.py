#!/usr/bin/env python

__author__ = "bitsofinfo"


from multiprocessing import Pool, Process
import random
import json
import pprint
import re
import os
import argparse
import getopt, sys
import datetime
import logging
import base64
import subprocess
import time

import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from http import HTTPStatus
from urllib.parse import urlparse
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily, REGISTRY
import concurrent.futures

def mkdirs(cmd_parts):
    # analyze each cmd part
    for part in cmd_parts:

        # if its a directory path
        # lets ensure the targets exist
        # as testssl.sh barfs on missing dir paths for outputs
        if '/' in part:

            # neither of these cmd parts convert to a dir
            if 'testssl.sh' in part or 'https://' in part:
                continue

            # default the target as is
            target_path = part

            # if the target_path if not fully qualified
            # lets scope it under outputdir_root
            if not part.startswith('/'):
                target_path = outputdir_root + "/" + part

            # ensure the path exists
            tmp_path,file = os.path.split(target_path)
            if not os.path.isdir(tmp_path):
                print("making dir: " + tmp_path)
                try:
                    os.makedirs(tmp_path)
                except Exception as e:
                    # we expect File exists due to concurrency
                    # other threads could be doing this at same time
                    if not 'File exists' in str(e):
                        raise e


def execTestsslCmd(args):
    testssl_cmd = args['testssl_cmd']
    timestamp = args['timestamp']
    testssl_path_if_missing = args['testssl_path_if_missing']

    cmd_result = { "success":False,
                   "orig_cmd":testssl_cmd,
                   "timestamp":timestamp,
                   "testssl_path_if_missing":testssl_path_if_missing }

    logging.info("Processing testssl_cmd: '%s'", testssl_cmd)

    try:
        # my working dir
        my_working_dir = os.path.dirname(os.path.realpath(__file__))

        # Where our output dir is
        # for path arguments in the command
        # that are relative and not absolute
        outputdir_root = None
        if 'outputdir_root' in args:
            outputdir_root = args['outputdir_root']

        # if outputdir_root is None
        # set it = working dir
        if outputdir_root is None:
            outputdir_root = my_working_dir

        # append ts subdir to outputdir_root
        outputdir_root += "/testssl_processor_output_" + timestamp

        # testssl.sh missing path?
        # prepend it with testssl_path_if_missing
        if testssl_cmd.startswith('testssl.sh'):
            if not os.path.exists(my_working_dir+"/testssl.sh"):
                testssl_cmd = testssl_path_if_missing + "/" + testssl_cmd

        # capture the actual cmd we are now executing
        # and note the current working dir
        cmd_result["actual_cmd"] = testssl_cmd
        cmd_result["cwd"] = outputdir_root

        # split the string into array
        cmd_parts = testssl_cmd.split()

        # make any required dirs
        # contained in paths embedded in the command
        # as testssl.sh does not make them if missing
        mkdirs(cmd_parts)

        # execute the command
        start = datetime.datetime.now()
        run_result = subprocess.run(testssl_cmd.split(),
                                    cwd=outputdir_root,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)

        logging.debug("Command finished: exit code: " + str(run_result.returncode) +
            " stdout.len:" +str(len(run_result.stdout)) +
            " stderr.len:" +str(len(run_result.stderr)) +
            " cmd: " + testssl_cmd)

        cmd_result["returncode"] = run_result.returncode
        cmd_result["stdout"] = run_result.stdout
        cmd_result["stderr"] = run_result.stderr

        if run_result.stderr is not None and len(run_result.stderr) > 0:
            cmd_result["success"] = False
        else:
            cmd_result["success"] = True

    except Exception as e:
        cmd_result["success"] = False
        cmd_result["exception"] = str(sys.exc_info()[:2])

    finally:
        cmd_result['exec_ms'] = (datetime.datetime.now() - start).total_seconds() * 1000

    return cmd_result


# a prometheus client_python
# custom "Collector": https://github.com/prometheus/client_python
# This classes collect() method is called periodically
# and it dumps the current state of the job_name_2_metrics_db
# database of metrics
class TestsslProcessor(object):

    # for controlling access to job_name_2_metrics_db
    lock = threading.RLock()

    # this is invoked by prometheus_client.core
    # on some sort of schedule or by request to get
    # latest metrics
    def collect(self):
        yield None


    # Will process the testssl_cmds file
    def processCmdsFile(self,testssl_cmds_file_path):
        print("CALLED " + testssl_cmds_file_path)
        # open the file
        testssl_cmds = []
        try:
            with open(testssl_cmds_file_path) as f:
                testssl_cmds = f.readlines()
                testssl_cmds = [x.strip() for x in testssl_cmds]
        except:
            print("Unexpected error:", sys.exc_info()[0])

        logging.info("Processing testssl_cmds: '%s'", testssl_cmds_file_path)

        threads = 8
        exec_pool = None

        # mthreaded...
        if (isinstance(threads,str)):
            threads = int(threads)

        # init pool
        exec_pool = Pool(threads)

        # timestamp for this event
        timestamp = datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')

        # process each command
        execTestsslCmd_args = []
        for cmd in testssl_cmds:
            execTestsslCmd_args.append({'outputdir_root':'testssl-output',
                                        'testssl_path_if_missing':'/Users/inter0p/Documents/omg/code/github.com/drwetter/testssl.sh',
                                        'testssl_cmd':cmd,
                                        'timestamp':timestamp})


        try:
            testssl_cmds_results = exec_pool.map(execTestsslCmd,execTestsslCmd_args)
            for result in testssl_cmds_results:
                pprint.pprint(result)
        except:
            print("Unexpected error:", sys.exc_info()[0])




class TestsslInputFileMonitor(FileSystemEventHandler):

    # We will feed new input files to this processor
    testssl_processor = None

    # max threads
    threads = 1

    # our Pool
    executor = None

    def set_threads(self, t):
        self.threads = t

    def on_created(self, event):
        super(TestsslInputFileMonitor, self).on_created(event)

        if not self.executor:
            self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.threads)

        if event.is_directory:
            return

        if 'testssl_cmds' in event.src_path:

            logging.info("Responding to creation of %s: %s", "file", event.src_path)

            # give write time to close....
            time.sleep(5)

            self.executor.submit(self.testssl_processor.processCmdsFile,event.src_path)



def init_watching(input_dir,listen_port,listen_addr,threads):

    # mthreaded...
    if (isinstance(threads,str)):
        threads = int(threads)

    # create watchdog to look for new files
    event_handler = TestsslInputFileMonitor()
    event_handler.set_threads(threads)

    # Create a TestsslProcessor to consume the testssl_cmds files
    event_handler.testssl_processor = TestsslProcessor()

    # schedule our file watchdog
    observer = Observer()
    observer.schedule(event_handler, input_dir, recursive=True)
    observer.start()

    #REGISTRY.register(event_handler.testssl_processor)

    # Start up the server to expose the metrics.
    #start_http_server(int(listen_port),addr=listen_addr)

    logging.info("Exposing testssl metrics for Prometheus at: http://%s:%s/metrics",listen_addr,str(listen_port))

    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()




###########################
# Main program
##########################
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-i', '--input-dir', dest='input_dir', default="./output", help="Directory path to recursively monitor for new '*servicecheckerdb*' json output files")
    parser.add_argument('-p', '--listen-port', dest='listen_port', default=8000, help="HTTP port to expose /metrics at")
    parser.add_argument('-a', '--listen-addr', dest='listen_addr', default='127.0.0.1', help="Address to expost metrics http server on")
    parser.add_argument('-l', '--log-file', dest='log_file', default=None, help="Path to log file, default None, STDOUT")
    parser.add_argument('-x', '--log-level', dest='log_level', default="DEBUG", help="log level, default DEBUG ")
    parser.add_argument('-d', '--threads', dest='threads', default=1, help="max threads for watchdog file processing")


    args = parser.parse_args()

    logging.basicConfig(level=logging.getLevelName(args.log_level),
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        filename=args.log_file,filemode='w')
    logging.Formatter.converter = time.gmtime


    init_watching(args.input_dir,args.listen_port,args.listen_addr,args.threads)
