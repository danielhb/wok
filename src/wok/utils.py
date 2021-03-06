#
# Project Wok
#
# Copyright IBM, Corp. 2013-2015
#
# Code derived from Project Kimchi
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#

import cherrypy
import grp
import os
import psutil
import pwd
import re
import sqlite3
import subprocess
import sys
import traceback
import xml.etree.ElementTree as ET
from cherrypy.lib.reprconf import Parser
from multiprocessing import Process, Queue
from threading import Timer

from wok.asynctask import AsyncTask
from wok.config import paths, PluginPaths
from wok.exception import InvalidParameter, TimeoutExpired


wok_log = cherrypy.log.error_log
task_id = 0


def get_next_task_id():
    global task_id
    task_id += 1
    return task_id


def get_task_id():
    global task_id
    return task_id


def add_task(target_uri, fn, objstore, opaque=None):
    id = get_next_task_id()
    AsyncTask(id, target_uri, fn, objstore, opaque)
    return id


def is_digit(value):
    if isinstance(value, int):
        return True
    elif isinstance(value, basestring):
        value = value.strip()
        return value.isdigit()
    else:
        return False


def _load_plugin_conf(name):
    plugin_conf = PluginPaths(name).conf_file
    if not os.path.exists(plugin_conf):
        cherrypy.log.error_log.error("Plugin configuration file %s"
                                     " doesn't exist." % plugin_conf)
        return
    try:
        return Parser().dict_from_file(plugin_conf)
    except ValueError as e:
        cherrypy.log.error_log.error("Failed to load plugin "
                                     "conf from %s: %s" %
                                     (plugin_conf, e.message))


def get_enabled_plugins():
    plugin_dir = paths.plugins_dir
    try:
        dir_contents = os.listdir(plugin_dir)
    except OSError:
        return
    for name in dir_contents:
        if os.path.isdir(os.path.join(plugin_dir, name)):
            plugin_config = _load_plugin_conf(name)
            try:
                if plugin_config['wok']['enable']:
                    yield (name, plugin_config)
            except (TypeError, KeyError):
                continue


def get_all_tabs():
    files = []

    for plugin, _ in get_enabled_plugins():
        files.append(os.path.join(PluginPaths(plugin).ui_dir,
                     'config/tab-ext.xml'))

    tabs = []
    for f in files:
        try:
            root = ET.parse(f)
        except (IOError):
            wok_log.debug("Unable to load %s", f)
            continue
        tabs.extend([t.text.lower() for t in root.getiterator('title')])

    return tabs


def import_class(class_path):
    module_name, class_name = class_path.rsplit('.', 1)
    try:
        mod = import_module(module_name, class_name)
        return getattr(mod, class_name)
    except (ImportError, AttributeError), e:
        raise ImportError(
            'Class %s can not be imported, '
            'error: %s' % (class_path, e.message)
        )


def import_module(module_name, class_name=''):
    return __import__(module_name, globals(), locals(), [class_name])


def run_command(cmd, timeout=None, silent=False, tee=None):
    """
    cmd is a sequence of command arguments.
    timeout is a float number in seconds.
    timeout default value is None, means command run without timeout.
    silent is bool, it will log errors using debug handler not error.
    silent default value is False.
    tee is a file path to store the output of the command, like 'tee' command.
    tee default value is None, means output will not be logged.
    """
    # subprocess.kill() can leave descendants running
    # and halting the execution. Using psutil to
    # get all descendants from the subprocess and
    # kill them recursively.
    def kill_proc(proc, timeout_flag):
        try:
            parent = psutil.Process(proc.pid)
            for child in parent.get_children(recursive=True):
                child.kill()
            # kill the process after no children is left
            proc.kill()
        except OSError:
            pass
        else:
            timeout_flag[0] = True

    # function to append the given msg into the log_file
    def tee_log(msg=None, log_file=None):
        if (msg is None) or (log_file is None):
            return

        try:
            f = open(log_file, 'a')
        except IOError as e:
            msg = "Failed to open file %s: " % log_file
            wok_log.error("%s %s", msg, e)
            return
        msg += '\n'
        try:
            f.write(msg)
        except TypeError:
            f.write(msg.encode('utf_8'))
        f.close()

    proc = None
    timer = None
    timeout_flag = [False]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        if timeout is not None:
            timer = Timer(timeout, kill_proc, [proc, timeout_flag])
            timer.setDaemon(True)
            timer.start()

        wok_log.debug("Run command: '%s'", " ".join(cmd))
        if tee is not None:
            if os.path.exists(tee):
                os.remove(tee)
            output = []
            while True:
                line = ""
                try:
                    line = proc.stdout.readline()
                    line = line.decode('utf_8')
                except Exception:
                    type, e, tb = sys.exc_info()
                    wok_log.error(e)
                    wok_log.error("The output of the command could not be "
                                  "decoded as %s\ncmd: %s\n line ignored: %s" %
                                  ('utf_8', cmd, repr(line)))
                    pass

                output.append(line)
                if not line:
                    break
                line = line.rstrip('\n\r')
                tee_log(line, tee)
            out = ''.join(output)
            error = proc.stderr.read()
        else:
            out, error = proc.communicate()

        if out:
            wok_log.debug("out:\n%s", out)

        if proc.returncode != 0:
            msg = "rc: %s error: %s returned from cmd: %s" %\
                  (proc.returncode, error, ' '.join(cmd))

            if silent:
                wok_log.debug(msg)

            else:
                wok_log.error(msg)
        elif error:
            wok_log.debug("error: %s returned from cmd: %s",
                          error, ' '.join(cmd))

        if timeout_flag[0]:
            msg = ("subprocess is killed by signal.SIGKILL for "
                   "timeout %s seconds" % timeout)
            wok_log.error(msg)

            msg_args = {'cmd': " ".join(cmd), 'seconds': str(timeout)}
            raise TimeoutExpired("WOKUTILS0002E", msg_args)

        return out, error, proc.returncode
    except TimeoutExpired:
        raise
    except OSError as e:
        msg = "Impossible to execute '%s'" % ' '.join(cmd)
        wok_log.debug("%s", msg)

        return None, "%s %s" % (msg, e), -1
    except Exception as e:
        msg = "Failed to run command: %s." % " ".join(cmd)
        msg = msg if proc is None else msg + "\n  error code: %s."
        wok_log.error("%s %s", msg, e)

        if proc:
            return out, error, proc.returncode
        else:
            return None, msg, -1
    finally:
        if timer and not timeout_flag[0]:
            timer.cancel()


def parse_cmd_output(output, output_items):
    res = []
    for line in output.split("\n"):
        if line:
            res.append(dict(zip(output_items, line.split())))
    return res


def patch_find_nfs_target(nfs_server):
    cmd = ["showmount", "--no-headers", "--exports", nfs_server]
    try:
        out = run_command(cmd, 10)[0]
    except TimeoutExpired:
        wok_log.warning("server %s query timeout, may not have any path "
                        "exported", nfs_server)
        return list()

    targets = parse_cmd_output(out, output_items=['target'])
    for target in targets:
        target['type'] = 'nfs'
        target['host_name'] = nfs_server
    return targets


def listPathModules(path):
    modules = set()
    for f in os.listdir(path):
        base, ext = os.path.splitext(f)
        if ext in ('.py', '.pyc', '.pyo'):
            modules.add(base)
    return sorted(modules)


def run_setfacl_set_attr(path, attr="r", user=""):
    set_user = ["setfacl", "--modify", "user:%s:%s" % (user, attr), path]
    out, error, ret = run_command(set_user)
    return ret == 0


def probe_file_permission_as_user(file, user):
    def probe_permission(q, file, user):
        uid = pwd.getpwnam(user).pw_uid
        gid = pwd.getpwnam(user).pw_gid
        gids = [g.gr_gid for g in grp.getgrall() if user in g.gr_mem]
        os.setgid(gid)
        os.setgroups(gids)
        os.setuid(uid)
        try:
            with open(file):
                q.put((True, None))
        except Exception as e:
            wok_log.debug(traceback.format_exc())
            q.put((False, e))

    queue = Queue()
    p = Process(target=probe_permission, args=(queue, file, user))
    p.start()
    p.join()
    return queue.get()


def get_next_clone_name(all_names, basename, name_suffix=''):
    """Find the next available name for a cloned resource.

    If any resource named "<basename>-clone-<number><name_suffix>" is found
    in "all_names", use the maximum "number" + 1; else, use 1.

    Arguments:
    all_names -- All existing names for the resource type. This list will
        be used to make sure the new name won't conflict with
        existing names.
    basename -- The name of the original resource.
    name_suffix -- The resource name suffix (optional). This parameter
        exist so that a resource named "foo.img" gets the name
        "foo-clone-1.img" instead of "foo.img-clone-1". If this parameter
        is used, the suffix should not be present in "basename".

    Return:
    A UTF-8 string in the format "<basename>-clone-<number><name_suffix>".
    """
    re_group_num = 'num'

    re_expr = u'%s-clone-(?P<%s>\d+)' % (basename, re_group_num)
    if name_suffix != '':
        re_expr = u'%s%s' % (re_expr, name_suffix)

    max_num = 0
    re_compiled = re.compile(re_expr)

    for n in all_names:
        match = re_compiled.match(n)
        if match is not None:
            max_num = max(max_num, int(match.group(re_group_num)))

    # increments the maximum "clone number" found
    new_name = u'%s-clone-%d' % (basename, max_num + 1)
    if name_suffix != '':
        new_name = new_name + name_suffix

    return new_name


def get_unique_file_name(all_names, name):
    """Find the next available, unique name for a file.

    If a file named "<name>" isn't found in "<all_names>", use that same
    "<name>".  There's no need to generate a new name in that case.

    If any file named "<name> (<number>)" is found in "all_names", use the
    maximum "number" + 1; else, use 1.

    Arguments:
    all_names -- All existing file names. This list will be used to make sure
        the new name won't conflict with existing names.
    name -- The name of the original file.

    Return:
    A string in the format "<name> (<number>)", or "<name>".
    """
    if name not in all_names:
        return name

    re_group_num = 'num'

    re_expr = u'%s \((?P<%s>\d+)\)' % (name, re_group_num)

    max_num = 0
    re_compiled = re.compile(re_expr)

    for n in all_names:
        match = re_compiled.match(n)
        if match is not None:
            max_num = max(max_num, int(match.group(re_group_num)))

    return u'%s (%d)' % (name, max_num + 1)


def servermethod(f):
    def wrapper(*args, **kwargs):
        server_state = str(cherrypy.engine.state)
        if server_state not in ["states.STARTED", "states.STARTING"]:
            return False
        return f(*args, **kwargs)
    return wrapper


def convert_data_size(value, from_unit, to_unit='B'):
    """Convert a data value from one unit to another unit
    (e.g. 'MiB' -> 'GiB').

    The data units supported by this function are made up of one prefix and one
    suffix. The valid prefixes are those defined in the SI (i.e. metric system)
    and those defined by the IEC, and the valid suffixes indicate if the base
    unit is bit or byte.
    Take a look at the tables below for the possible values:

    Prefixes:

    ==================================     ===================================
    PREFIX (SI) | DESCRIPTION | VALUE      PREFIX (IEC) | DESCRIPTION | VALUE
    ==================================     ===================================
    k           | kilo        | 1000       Ki           | kibi        | 1024
    ----------------------------------     -----------------------------------
    M           | mega        | 1000^2     Mi           | mebi        | 1024^2
    ----------------------------------     -----------------------------------
    G           | giga        | 1000^3     Gi           | gibi        | 1024^3
    ----------------------------------     -----------------------------------
    T           | tera        | 1000^4     Ti           | tebi        | 1024^4
    ----------------------------------     -----------------------------------
    P           | peta        | 1000^5     Pi           | pebi        | 1024^5
    ----------------------------------     -----------------------------------
    E           | exa         | 1000^6     Ei           | exbi        | 1024^6
    ----------------------------------     -----------------------------------
    Z           | zetta       | 1000^7     Zi           | zebi        | 1024^7
    ----------------------------------     -----------------------------------
    Y           | yotta       | 1000^8     Yi           | yobi        | 1024^8
    ==================================     ===================================

    Suffixes:

    =======================
    SUFFIX | DESCRIPTION
    =======================
    b      | bit
    -----------------------
    B      | byte (default)
    =======================

    See http://en.wikipedia.org/wiki/Binary_prefix for more details on
    those units.

    If a wrong unit is provided, an error will be raised.

    Examples:
        convert_data_size(5, 'MiB', 'KiB') -> 5120.0
        convert_data_size(5, 'MiB', 'M')   -> 5.24288
        convert_data_size(5, 'MiB', 'GiB') -> 0.0048828125
        convert_data_size(5, 'MiB', 'Tb')  -> 4.194304e-05
        convert_data_size(5, 'MiB')        -> 5242880.0
        convert_data_size(5, 'mib')        -> #ERROR# (invalid from_unit)

    Parameters:
    value -- the value to be converted, in the unit specified by 'from_unit'.
             this parameter can be of any type which can be cast to float
             (e.g. int, float, str).
    from_unit -- the unit of 'value', as described above.
    to_unit -- the unit of the return value, as described above.

    Return:
    A float number representing 'value' (in 'from_unit') converted
    to 'to_unit'.
    """
    SI_PREFIXES = ['k', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y']
    # The IEC prefixes are the equivalent SI prefixes + 'i'
    # but, exceptionally, 'k' becomes 'Ki' instead of 'ki'.
    IEC_PREFIXES = map(lambda p: 'Ki' if p == 'k' else p + 'i', SI_PREFIXES)
    PREFIXES_BY_BASE = {1000: SI_PREFIXES,
                        1024: IEC_PREFIXES}

    SUFFIXES_WITH_MULT = {'b': 1,
                          'B': 8}
    DEFAULT_SUFFIX = 'B'

    if not from_unit:
        raise InvalidParameter('WOKUTILS0005E', {'unit': from_unit})
    if not to_unit:
        raise InvalidParameter('WOKUTILS0005E', {'unit': to_unit})

    # set the default suffix
    if from_unit[-1] not in SUFFIXES_WITH_MULT:
        from_unit += DEFAULT_SUFFIX
    if to_unit[-1] not in SUFFIXES_WITH_MULT:
        to_unit += DEFAULT_SUFFIX

    # split prefix and suffix for better parsing
    from_p = from_unit[:-1]
    from_s = from_unit[-1]
    to_p = to_unit[:-1]
    to_s = to_unit[-1]

    # validate parameters
    try:
        value = float(value)
    except TypeError:
        raise InvalidParameter('WOKUTILS0004E', {'value': value})
    if from_p != '' and from_p not in (SI_PREFIXES + IEC_PREFIXES):
        raise InvalidParameter('WOKUTILS0005E', {'unit': from_unit})
    if from_s not in SUFFIXES_WITH_MULT:
        raise InvalidParameter('WOKUTILS0005E', {'unit': from_unit})
    if to_p != '' and to_p not in (SI_PREFIXES + IEC_PREFIXES):
        raise InvalidParameter('WOKUTILS0005E', {'unit': to_unit})
    if to_s not in SUFFIXES_WITH_MULT:
        raise InvalidParameter('WOKUTILS0005E', {'unit': to_unit})

    # if the units are the same, return the input value
    if from_unit == to_unit:
        return value

    # convert 'value' to the most basic unit (bits)...
    bits = value

    for suffix, mult in SUFFIXES_WITH_MULT.iteritems():
        if from_s == suffix:
            bits *= mult
            break

    if from_p != '':
        for base, prefixes in PREFIXES_BY_BASE.iteritems():
            for i, p in enumerate(prefixes):
                if from_p == p:
                    bits *= base**(i + 1)
                    break

    # ...then convert the value in bits to the destination unit
    ret = bits

    for suffix, mult in SUFFIXES_WITH_MULT.iteritems():
        if to_s == suffix:
            ret /= float(mult)
            break

    if to_p != '':
        for base, prefixes in PREFIXES_BY_BASE.iteritems():
            for i, p in enumerate(prefixes):
                if to_p == p:
                    ret /= float(base)**(i + 1)
                    break

    return ret


def get_objectstore_fields(objstore=None):
    """
        Return a list with all fields from the objectstore.
    """
    if objstore is None:
        wok_log.error("No objectstore set up.")
        return None
    conn = sqlite3.connect(objstore, timeout=10)
    cursor = conn.cursor()
    schema_fields = []
    sql = "PRAGMA table_info('objects')"
    cursor.execute(sql)
    for row in cursor.fetchall():
        schema_fields.append(row[1])
    return schema_fields


def upgrade_objectstore_schema(objstore=None, field=None):
    """
        Add a new column (of type TEXT) in the objectstore schema.
    """
    if (field or objstore) is None:
        wok_log.error("Cannot upgrade objectstore schema.")
        return False

    if field in get_objectstore_fields(objstore):
        # field already exists in objectstore schema. Nothing to do.
        return False
    try:
        conn = sqlite3.connect(objstore, timeout=10)
        cursor = conn.cursor()
        sql = "ALTER TABLE objects ADD COLUMN %s TEXT" % field
        cursor.execute(sql)
        wok_log.info("Objectstore schema sucessfully upgraded: %s" % objstore)
        conn.close()
    except sqlite3.Error, e:
        if conn:
            conn.rollback()
            conn.close()
        wok_log.error("Cannot upgrade objectstore schema: %s" % e.args[0])
        return False
    return True
