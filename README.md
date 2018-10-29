# testssl.sh-processor

This project is intended to provide mass concurrent invocations of the great SSL/TLS
testing tool https://github.com/drwetter/testssl.sh via command files.

## testssl_processor.py

Provides a long lived process that monitors a directory (via [watchdog](https://github.com/gorakhargosh/watchdog))
for `testssl.sh` command files. As new files appear within the `--input-dir` containing the `--filename-filter`
they are consumed and evaluated for `testssl.sh` commands, one per line. Each `testssl.sh` command is processed in a separate thread and processing results are logged to a YAML or JSON result file. under an `--output-dir`. The actual output from each invoked `testssl.sh` invocation is also written to disk scoped within a timestamped output directory scoped within the `--output-dir` (if specified per command in the command file via the `--*file` output arguments)
