import ConfigParser
import gettext
import os
import re
import random
import shutil
import sqlite3
from webob import Request
from subprocess import *

GETTEXT_DOMAIN = "retrace-server"

REQUIRED_FILES = ["coredump", "executable", "package"]

DF_BIN = "/bin/df"
DU_BIN = "/usr/bin/du"
GZIP_BIN = "/usr/bin/gzip"
TAR_BIN = "/bin/tar"
XZ_BIN = "/usr/bin/xz"

#characters, numbers, dash (utf-8, iso-8859-2 etc.)
INPUT_CHARSET_PARSER = re.compile("^([a-zA-Z0-9\-]+)(,.*)?$")
#en_GB, sk-SK, cs, fr etc.
INPUT_LANG_PARSER = re.compile("^([a-z]{2}([_\-][A-Z]{2})?)(,.*)?$")
#characters allowed by Fedora Naming Guidelines
INPUT_PACKAGE_PARSER = re.compile("^[abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\-\.\_\+]+$")

PACKAGE_PARSER = re.compile("^(.+)-([0-9]+(\.[0-9]+)*-[0-9]+)\.([^-]+)$")
DF_OUTPUT_PARSER = re.compile("^([^ ^\t]*)[ \t]+([0-9]+)[ \t]+([0-9]+)[ \t]+([0-9]+)[ \t]+([0-9]+%)[ \t]+(.*)$")
DU_OUTPUT_PARSER = re.compile("^([0-9]+)")
URL_PARSER = re.compile("^/([0-9]+)/?")
WORKER_RUNNING_PARSER = re.compile("^[ \t]*([0-9]+)[ \t]+[0-9]+[ \t]+([^ ^\t]+)[ \t]+.*worker\.py ([0-9]+)$")

HANDLE_ARCHIVE = {
  "application/x-xz-compressed-tar": {
    "unpack": [TAR_BIN, "xJf"],
    "size": ([XZ_BIN, "--list", "--robot"], re.compile("^totals[ \t]+[0-9]+[ \t]+[0-9]+[ \t]+[0-9]+[ \t]+([0-9]+).*")),
  },

  "application/x-gzip": {
    "unpack": [TAR_BIN, "xzf"],
    "size": ([GZIP_BIN, "--list"], re.compile("^[^0-9]*[0-9]+[^0-9]+([0-9]+).*$")),
  },

  "application/x-tar": {
    "unpack": [TAR_BIN, "xf"],
    "size": (["ls", "-l"], re.compile("^[ \t]*[^ ^\t]+[ \t]+[^ ^\t]+[ \t]+[^ ^\t]+[ \t]+[^ ^\t]+[ \t]+([0-9]+).*$")),
  },
}

TASKPASS_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

CONFIG_FILE = "/etc/retrace-server.conf"
CONFIG = {
  "TaskIdLength": 9,
  "TaskPassLength": 32,
  "MaxParallelTasks": 10,
  "MaxPackedSize": 30,
  "MaxUnpackedSize": 600,
  "MinStorageLeft": 10240,
  "DeleteTaskAfter": 120,
  "LogDir": "/var/log/retrace-server",
  "RepoDir": "/var/cache/retrace-server",
  "SaveDir": "/var/spool/retrace-server",
  "WorkDir": "/tmp/retrace-server",
  "UseWorkDir": False,
  "DBFile": "stats.db",
}

STATUS_ANALYZE, STATUS_INIT, STATUS_BACKTRACE, STATUS_CLEANUP, \
STATUS_STATS, STATUS_FINISHING, STATUS_SUCCESS, STATUS_FAIL = xrange(8)

STATUS = [
  "Analyzing crash data",
  "Initializing virtual root",
  "Generating backtrace",
  "Cleaning up virtual root",
  "Saving crash statistics",
  "Finishing task",
  "Retrace job finished successfully",
  "Retrace job failed",
]

def lock(lockfile):
    try:
        if not os.path.isfile(lockfile):
            open(lockfile, "w").close()
    except:
        return False

    return True

def unlock(lockfile):
    try:
        if os.path.getsize(lockfile) == 0:
            os.unlink(lockfile)
    except:
        return False

    return True

def read_config():
    parser = ConfigParser.ConfigParser()
    parser.read(CONFIG_FILE)
    for key in CONFIG.keys():
        vartype = type(CONFIG[key])
        if vartype is int:
            get = parser.getint
        elif vartype is bool:
            get = parser.getboolean
        elif vartype is float:
            get = parser.getfloat
        else:
            get = parser.get

        try:
            CONFIG[key] = get("retrace", key)
        except:
            pass

def free_space(path):
    child = Popen([DF_BIN, "-B", "1", path], stdout=PIPE)
    lines = child.communicate()[0].split("\n")
    for line in lines:
        match = DF_OUTPUT_PARSER.match(line)
        if match:
            return int(match.group(4))

    return None

def dir_size(path):
    child = Popen([DU_BIN, "-sb", path], stdout=PIPE)
    lines = child.communicate()[0].split("\n")
    for line in lines:
        match = DU_OUTPUT_PARSER.match(line)
        if match:
            return int(match.group(1))

    return 0

def unpacked_size(archive, mime):
    command, parser = HANDLE_ARCHIVE[mime]["size"]
    child = Popen(command + [archive], stdout=PIPE)
    lines = child.communicate()[0].split("\n")
    for line in lines:
        match = parser.match(line)
        if match:
            return int(match.group(1))

    return None

def guess_arch(coredump_path):
    child = Popen(["file", coredump_path], stdout=PIPE)
    output = child.communicate()[0]

    if "x86-64" in output:
        return "x86_64"

    if "80386" in output:
        return "i386"

    return None

def guess_release(package, plugins):
    for plugin in plugins:
        match = plugin.guessparser.search(package)
        if match:
            return plugin.distribution, match.group(1)

    return None, None

def parse_http_gettext(lang, charset):
    result = lambda x: x
    lang_match = INPUT_LANG_PARSER.match(lang)
    charset_match = INPUT_CHARSET_PARSER.match(charset)
    if lang_match and charset_match:
        try:
            result = gettext.translation(GETTEXT_DOMAIN,
                                         languages=[lang_match.group(1)],
                                         codeset=charset_match.group(1)).gettext
        except:
            pass

    return result

def run_gdb(savedir):
    try:
        exec_file = open("%s/crash/executable" % savedir, "r")
        executable = exec_file.read().replace("'", "").replace("\"", "")
        exec_file.close()
    except:
        return ""

    mockr = "../../%s/mock" % savedir

    chmod = call(["mock", "shell", "-r", mockr, "--",
                  "/bin/chmod", "777", executable])
    if chmod != 0:
        return ""

    child = Popen(["mock", "shell", "-r", mockr, "--",
                   "su", "mockbuild", "-c",
                   "\" gdb -batch"
                   " -ex 'file %s'"
                   " -ex 'core-file /var/spool/abrt/crash/coredump'"
                   " -ex 'thread apply all backtrace 2048 full'"
                   " -ex 'info sharedlib'"
                   " -ex 'print (char*)__abort_msg'"
                   " -ex 'print (char*)__glib_assert_msg'"
                   " -ex 'info registers'"
                   " -ex 'disassemble' \"" % executable,
                   # redirect GDB's stderr, ignore mock's stderr
                   "2>&1"], stdout=PIPE)

    backtrace = child.communicate()[0]

    return backtrace

def gen_task_password(taskdir):
    generator = random.SystemRandom()
    taskpass = ""
    for j in xrange(CONFIG["TaskPassLength"]):
        taskpass += generator.choice(TASKPASS_ALPHABET)

    try:
        passfile = open("%s/password" % taskdir, "w")
        passfile.write(taskpass)
        passfile.close()
    except:
        return None

    return taskpass

def get_task_est_time(taskdir):
    return 180

def new_task():
    i = 0
    newdir = CONFIG["SaveDir"]
    while os.path.exists(newdir) and i < 50:
        i += 1
        taskid = random.randint(pow(10, CONFIG["TaskIdLength"] - 1), pow(10, CONFIG["TaskIdLength"]) - 1)
        newdir = "%s/%d" % (CONFIG["SaveDir"], taskid)

    try:
        os.mkdir(newdir)
        taskpass = gen_task_password(newdir)
        if not taskpass:
            shutil.rmtree(newdir)
            raise Exception

        return taskid, taskpass, newdir
    except:
        return None, None, None

def unpack(archive, mime):
    retcode = call(HANDLE_ARCHIVE[mime]["unpack"] + [archive])
    return retcode

def response(start_response, status, body="", extra_headers=[]):
    start_response(status, [("Content-Type", "text/plain"), ("Content-Length", "%d" % len(body))] + extra_headers)
    return [body]

def get_active_tasks():
    tasks = []
    if CONFIG["UseWorkDir"]:
        tasksdir = CONFIG["WorkDir"]
    else:
        tasksdir = CONFIG["SaveDir"]

    for filename in os.listdir(tasksdir):
        if len(filename) != CONFIG["TaskIdLength"]:
            continue

        try:
            taskid = int(filename)
        except:
            continue

        path = "%s/%s" % (tasksdir, filename)
        if os.path.isdir(path) and not os.path.isfile("%s/retrace_log" % path):
            tasks.append(taskid)

    return tasks

def run_ps():
    child = Popen(["ps", "-eo", "pid,ppid,etime,cmd"], stdout=PIPE)
    lines = child.communicate()[0].split("\n")

    return lines

def get_running_tasks(ps_output=None):
    if not ps_output:
        ps_output = run_ps()

    result = []

    for line in ps_output:
        match = WORKER_RUNNING_PARSER.match(line)
        if match:
            result.append((int(match.group(1)), int(match.group(3)), match.group(2)))

    return result

def get_process_tree(pid, ps_output):
    result = [pid]

    parser = re.compile("^([0-9]+)[ \t]+(%d).*$" % pid)

    for line in ps_output:
        match = parser.match(line)
        if match:
            pid = int(match.group(1))
            result.extend(get_process_tree(pid, ps_output))

    return result

def kill_process_and_childs(process_id, ps_output=None):
    result = True

    if not ps_output:
        ps_output = run_ps()

    for pid in get_process_tree(process_id, ps_output):
        try:
            os.kill(pid, 9)
        except OSError, ex:
            result = False

    return result

def cleanup_task(taskid, gc=True):
    null = open("/dev/null", "w")

    savedir = "%s/%d" % (CONFIG["SaveDir"], taskid)
    if os.path.isfile("%s/mock.cfg" % savedir):
        call(["mock", "-r", "../../%s/mock" % savedir, "--scrub=all"],
             stdout=null, stderr=null)

    try:
        shutil.rmtree("%s/crash" % savedir)
        os.remove("%s/mock.cfg" % savedir)
    except:
        pass

    rawlog = "%s/log" % savedir
    newlog = "%s/retrace_log" % savedir
    if os.path.isfile(rawlog):
        try:
            os.rename(rawlog, newlog)
        except:
            pass

    if gc:
        try:
            log = open(newlog, "a")
            log.write("Killed by garbage collector\n")
            log.close()
        except:
            pass

    null.close()

def init_crashstats_db():
    try:
        con = sqlite3.connect("%s/%s" % (CONFIG["SaveDir"], CONFIG["DBFile"]))
        query = con.cursor()
        query.execute("""
          CREATE TABLE IF NOT EXISTS
          retracestats(
            taskid INT NOT NULL,
            package VARCHAR(255) NOT NULL,
            version VARCHAR(16) NOT NULL,
            release VARCHAR(16) NOT NULL,
            arch VARCHAR(8) NOT NULL,
            starttime INT NOT NULL,
            duration INT NOT NULL,
            prerunning TINYINT NOT NULL,
            postrunning TINYINT NOT NULL,
            chrootsize BIGINT NOT NULL
          )
        """)
        con.commit()
        con.close()

        return True
    except:
        return False

def save_crashstats(crashstats):
    try:
        con = sqlite3.connect("%s/%s" % (CONFIG["SaveDir"], CONFIG["DBFile"]))
        query = con.cursor()
        query.execute("""
          INSERT INTO retracestats(taskid, package, version, release, arch,
          starttime, duration, prerunning, postrunning, chrootsize)
          VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          """,
          (crashstats["taskid"], crashstats["package"], crashstats["version"],
           crashstats["release"], crashstats["arch"], crashstats["starttime"],
           crashstats["duration"], crashstats["prerunning"],
           crashstats["postrunning"], crashstats["chrootsize"])
        )
        con.commit()
        con.close()

        return True
    except:
        return False

class logger():
    def __init__(self, taskid):
        "Starts logging into savedir."
        self._logfile = open("%s/%s/log" % (CONFIG["SaveDir"], taskid), "w")

    def write(self, msg):
        "Writes msg into log file."
        if not self._logfile.closed:
            self._logfile.write(msg)
            self._logfile.flush()

    def close(self):
        "Finishes logging and renames file to retrace_log."
        if not self._logfile.closed:
            self._logfile.close()
            os.rename(self._logfile.name, self._logfile.name.replace("/log", "/retrace_log"))

### read config on import ###
read_config()
