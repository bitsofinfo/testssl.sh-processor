#!/usr/bin/env python

__author__ = "bitsofinfo"


from multiprocessing import Pool, Process
import json
import pprint
import yaml
import re
import os
import argparse
import sys
import datetime
import logging
import subprocess
import time

import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import concurrent.futures

# Given an array of testssl.sh command parts
# and a target root output_dir, this will will
# do its best to detect filesystem path arguments
# and make sure those directories exist as when
# testssl.sh executes it expects all --*file output
# arguments to reference pre-existing paths
def mkdirs(cmd_parts, outputdir_root):
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


# Takes a map/dict of arguments and executes the given testssl.sh command
#
#  - testssl_cmd = the actual testssl.sh command to execute
#  - a unique timestamp for when the source command file event was received
#  - system PATH to where the testssl.sh command lives if not present on the cmd itself
#
#  - Returns an dict of result object information for the command executed
def execTestsslCmd(args):
    testssl_cmd = args['testssl_cmd']
    timestamp = args['timestamp']
    my_working_dir = args['my_working_dir']
    testssl_path_if_missing = args['testssl_path_if_missing']

    cmd_result = { "success":False,
                   "orig_cmd":testssl_cmd,
                   "timestamp":timestamp,
                   "testssl_path_if_missing":testssl_path_if_missing }

    logging.info("Processing testssl_cmd: '%s'", testssl_cmd)

    start = datetime.datetime.now()

    try:
        # Where our output dir is
        # for path arguments in the command
        # that are relative and not absolute
        outputdir_root = args['outputdir_root']

        # testssl.sh missing path?
        # prepend it with testssl_path_if_missing
        if testssl_cmd.startswith('testssl.sh') or testssl_cmd.startswith('./testssl.sh'):
            # my_dir
            my_dir = os.path.realpath(__file__)
            my_dir,file = os.path.split(my_dir)
            if not os.path.exists(my_dir+"/testssl.sh") or not os.path.isfile(my_dir+"/testssl.sh"):
                if testssl_path_if_missing.startswith('./'):
                    testssl_path_if_missing = my_dir + "/" + testssl_path_if_missing.replace("./","")
                    testssl_cmd = testssl_path_if_missing + "/" + testssl_cmd.replace("./","")

        # capture the actual cmd we are now executing
        # and note the current working dir
        cmd_result["actual_cmd"] = testssl_cmd
        cmd_result["cwd"] = outputdir_root

        # split the string into array
        cmd_parts = testssl_cmd.split()

        # make any required dirs
        # contained in paths embedded in the command
        # as testssl.sh does not make them if missing
        mkdirs(cmd_parts,outputdir_root)

        # execute the command
        run_result = subprocess.run(testssl_cmd.split(),
                                    cwd=outputdir_root,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)

        logging.debug("Command finished: exit code: " + str(run_result.returncode) +
            " stdout.len:" +str(len(run_result.stdout)) +
            " stderr.len:" +str(len(run_result.stderr)) +
            " cmd: " + testssl_cmd)

        cmd_result["returncode"] = run_result.returncode
        cmd_result["stdout"] = run_result.stdout.decode('utf-8')
        cmd_result["stderr"] = run_result.stderr.decode('utf-8')

        if run_result.stderr is not None and len(run_result.stderr) > 0:
            cmd_result["success"] = False
        else:
            cmd_result["success"] = True

    except Exception as e:
        logging.exception("Unexpected error in spawning testssl.sh command: " + testssl_cmd + " error:" + str(sys.exc_info()[:2]))
        cmd_result["success"] = False
        cmd_result["exception"] = str(sys.exc_info()[:2])

    finally:
        cmd_result['exec_ms'] = (datetime.datetime.now() - start).total_seconds() * 1000

    return cmd_result


class TestsslProcessor(object):

    # for controlling access to job_name_2_metrics_db
    lock = threading.RLock()

    # total threads = total amount of commands
    # per file that can be processed concurrently
    threads = 1

    # result format/filename prefix
    testssl_path_if_missing = None
    output_dir = "./testssl_processor_output"
    result_filename_prefix = 'testssl_processor_result'
    result_format = 'json'

    # this is invoked by prometheus_client.core
    # on some sort of schedule or by request to get
    # latest metrics
    def collect(self):
        yield None


    # Will process the testssl_cmds file
    def processCmdsFile(self,testssl_cmds_file_path):

        # open the file
        testssl_cmds = []
        try:
            with open(testssl_cmds_file_path) as f:
                testssl_cmds = f.readlines()
                testssl_cmds = [x.strip() for x in testssl_cmds]
        except:
            logging.exception("Unexpected error in open("+testssl_cmds_file_path+"): " + str(sys.exc_info()[0]))

        try:
            logging.info("Processing testssl_cmds: '%s'", testssl_cmds_file_path)

            exec_pool = None

            # init pool
            exec_pool = Pool(self.threads)

            # timestamp for this event
            timestamp = datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')

            # my working dir
            my_working_dir = os.path.dirname(os.path.realpath(__file__));

            # append ts subdir to outputdir_root
            outputdir_root = self.output_dir + "/testssl_processor_output_" + timestamp

            # ensure exists
            os.makedirs(outputdir_root)

            # process each command
            execTestsslCmd_args = []
            for cmd in testssl_cmds:
                execTestsslCmd_args.append({'outputdir_root':outputdir_root,
                                            'testssl_path_if_missing':self.testssl_path_if_missing,
                                            'testssl_cmd':cmd,
                                            'my_working_dir':my_working_dir,
                                            'timestamp':timestamp})


            try:
                testssl_cmds_results = exec_pool.map(execTestsslCmd,execTestsslCmd_args)

                # log the processor execution results
                output_filename = outputdir_root + "/" + self.result_filename_prefix + "_" + timestamp
                print(self.result_format)
                if self.result_format == 'yaml':
                    output_filename += ".yaml"
                else:
                    output_filename += ".json"

                # to json
                if output_filename is not None:
                    with open(output_filename, 'w') as outfile:
                        if self.result_format == 'json':
                            json.dump(testssl_cmds_results, outfile, indent=4)
                        else:
                            yaml.dump(testssl_cmds_results, outfile, default_flow_style=False)

                        logging.debug("Event %s Testssl processor result written to: %s",timestamp,output_filename)

            except Exception as e:
                logging.exception("Unexpected error in exec_pool.map -> execTestsslCmd(): " + str(sys.exc_info()[0]))

        except Exception as e:
            logging.exception("Unexpected error: " + str(sys.exc_info()[0]))



class TestsslInputFileMonitor(FileSystemEventHandler):

    # We will feed new input files to this processor
    testssl_processor = None

    # max threads
    threads = 1

    # our Pool
    executor = None

    # Filter to match relevent paths in events received
    filename_filter = 'testssl_cmds'

    def set_threads(self, t):
        self.threads = t

    def on_created(self, event):
        super(TestsslInputFileMonitor, self).on_created(event)

        if not self.executor:
            self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.threads)

        if event.is_directory:
            return

        if self.filename_filter in event.src_path:

            logging.info("Responding to creation of %s: %s", "file", event.src_path)

            # give write time to close....
            time.sleep(5)

            self.executor.submit(self.testssl_processor.processCmdsFile,event.src_path)



def init_watching(input_dir,
                 output_dir,
                 filename_filter,
                 watchdog_threads,
                 testssl_threads,
                 result_filename_prefix,
                 result_format,
                 testssl_path_if_missing):

    # mthreaded...
    if (isinstance(watchdog_threads,str)):
        watchdog_threads = int(watchdog_threads)

    # create watchdog to look for new files
    event_handler = TestsslInputFileMonitor()
    event_handler.filename_filter = filename_filter
    event_handler.set_threads(watchdog_threads)

    # Create a TestsslProcessor to consume the testssl_cmds files
    event_handler.testssl_processor = TestsslProcessor()
    event_handler.testssl_processor.testssl_path_if_missing = testssl_path_if_missing
    event_handler.testssl_processor.output_dir = output_dir
    event_handler.testssl_processor.result_filename_prefix = result_filename_prefix
    event_handler.testssl_processor.result_format = result_format

    # give the processor the total number of threads to use
    # for processing testssl.sh cmds concurrently
    if (isinstance(testssl_threads,str)):
        testssl_threads = int(testssl_threads)
    event_handler.testssl_processor.threads = testssl_threads

    # schedule our file watchdog
    observer = Observer()
    observer.schedule(event_handler, input_dir, recursive=True)
    observer.start()

    logging.info("Monitoring for new testssl_cmds files at: %s with filename filter: %s",input_dir,filename_filter)

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
    parser.add_argument('-i', '--input-dir', dest='input_dir', default="./input", help="Directory path to recursively monitor for new `--filename-filter` testssl.sh command files")
    parser.add_argument('-O', '--output-dir', dest='output_dir', default="./testssl_processor_output", help="Directory path to place all processor output, and testssl.sh output files to if relative paths are in command files. If absoluate paths are in testssl.sh command files they will be respected and only processor putput will go into --output-dir")
    parser.add_argument('-m', '--testssl-path-if-missing', dest='testssl_path_if_missing', default="./testssl.sh", help="If the testssl.sh commands in the command files do not reference an absolute path to the testssl.sh command, it assumes its already on the PATH or in the current working directory of the processor. Otherwise you can specify the PATH to it with this argument")
    parser.add_argument('-f', '--filename-filter', dest='filename_filter', default="testssl_cmds", help="Only react to filenames in --input-dir that contain the string --filename-filter, default 'testssl_cmds'")
    parser.add_argument('-o', '--result-filename-prefix', dest='result_filename_prefix', default="testssl_processor_result", help="processor execution result filename prefix")
    parser.add_argument('-q', '--result-format', dest='result_format', default="json", help="processor result filename format, json or yaml")
    parser.add_argument('-l', '--log-file', dest='log_file', default=None, help="Path to log file, default None, STDOUT")
    parser.add_argument('-x', '--log-level', dest='log_level', default="DEBUG", help="log level, default DEBUG ")
    parser.add_argument('-w', '--watchdog-threads', dest='watchdog_threads', default=1, help="max threads for watchdog file processing, default 1")
    parser.add_argument('-t', '--testssl-threads', dest='testssl_threads', default=10, help="for each watchdog file event, the maximum number of commands to be processed concurrently by testssl.sh invocations, default 10")

    args = parser.parse_args()

    logging.basicConfig(level=logging.getLevelName(args.log_level),
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        filename=args.log_file,filemode='w')
    logging.Formatter.converter = time.gmtime


    init_watching(args.input_dir,
                  args.output_dir,
                  args.filename_filter,
                  args.watchdog_threads,
                  args.testssl_threads,
                  args.result_filename_prefix,
                  args.result_format,
                  args.testssl_path_if_missing)
