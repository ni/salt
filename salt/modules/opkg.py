"""
Support for Opkg

.. important::
    If you feel that Salt should be using this module to manage packages on a
    minion, and it is using a different module (or gives an error similar to
    *'pkg.install' is not available*), see :ref:`here
    <module-provider-override>`.

.. versionadded:: 2016.3.0

.. note::

    For version comparison support on opkg < 0.3.4, the ``opkg-utils`` package
    must be installed.

"""

import copy
import errno
import logging
import os
import pathlib
import re
import subprocess
import time
import select
from fcntl import fcntl, F_GETFL, F_SETFL
import shlex

import salt.utils.args
import salt.utils.data
import salt.utils.files
import salt.utils.itertools
import salt.utils.path
import salt.utils.pkg
import salt.utils.stringutils
import salt.utils.versions
from salt.exceptions import CommandExecutionError, MinionError, SaltInvocationError

REPO_REGEXP = r'^#?\s*(src|src/gz)\s+([^\s<>]+|"[^<>]+")\s+[^\s<>]+'
OPKG_CONFDIR = "/etc/opkg"
ATTR_MAP = {
    "Architecture": "arch",
    "Homepage": "url",
    "Installed-Time": "install_date_time_t",
    "Maintainer": "packager",
    "Package": "name",
    "Section": "group",
}

log = logging.getLogger(__name__)

# Define the module's virtual name
__virtualname__ = "pkg"

NILRT_RESTARTCHECK_STATE_PATH = "/var/lib/salt/restartcheck_state"
PACKAGES_TO_INSTALL_DETACHED = ["ni-sysmgmt-salt-minion-support"]

def _get_nisysapi_conf_d_path():
    return "/usr/lib/{}/nisysapi/conf.d/experts/".format(
        "arm-linux-gnueabi"
        if "arm" in __grains__.get("cpuarch")
        else "x86_64-linux-gnu"
    )


def _update_nilrt_restart_state():
    """
    NILRT systems determine whether to reboot after various package operations
    including but not limited to kernel module installs/removals by checking
    specific file md5sums & timestamps. These files are touched/modified by
    the post-install/post-remove functions of their respective packages.

    The opkg module uses this function to store/update those file timestamps
    and checksums to be used later by the restartcheck module.

    """
    # TODO: This stat & md5sum should be replaced with _fingerprint_file call -W. Werner, 2020-08-18
    uname = __salt__["cmd.run_stdout"]("uname -r")
    __salt__["cmd.shell"](
        "stat -c %Y /lib/modules/{}/modules.dep >{}/modules.dep.timestamp".format(
            uname, NILRT_RESTARTCHECK_STATE_PATH
        )
    )
    __salt__["cmd.shell"](
        "md5sum /lib/modules/{}/modules.dep >{}/modules.dep.md5sum".format(
            uname, NILRT_RESTARTCHECK_STATE_PATH
        )
    )

    # We can't assume nisysapi.ini always exists like modules.dep
    nisysapi_path = "/usr/local/natinst/share/nisysapi.ini"
    if os.path.exists(nisysapi_path):
        # TODO: This stat & md5sum should be replaced with _fingerprint_file call -W. Werner, 2020-08-18
        __salt__["cmd.shell"](
            "stat -c %Y {} >{}/nisysapi.ini.timestamp".format(
                nisysapi_path, NILRT_RESTARTCHECK_STATE_PATH
            )
        )
        __salt__["cmd.shell"](
            "md5sum {} >{}/nisysapi.ini.md5sum".format(
                nisysapi_path, NILRT_RESTARTCHECK_STATE_PATH
            )
        )

    # Expert plugin files get added to a conf.d dir, so keep track of the total
    # no. of files, their timestamps and content hashes
    nisysapi_conf_d_path = _get_nisysapi_conf_d_path()

    if os.path.exists(nisysapi_conf_d_path):
        with salt.utils.files.fopen(
            "{}/sysapi.conf.d.count".format(NILRT_RESTARTCHECK_STATE_PATH), "w"
        ) as fcount:
            fcount.write(str(len(os.listdir(nisysapi_conf_d_path))))

        for fexpert in os.listdir(nisysapi_conf_d_path):
            _fingerprint_file(
                filename=pathlib.Path(nisysapi_conf_d_path, fexpert),
                fingerprint_dir=pathlib.Path(NILRT_RESTARTCHECK_STATE_PATH),
            )


def _fingerprint_file(*, filename, fingerprint_dir):
    """
    Compute stat & md5sum hash of provided ``filename``. Store
    the hash and timestamp in ``fingerprint_dir``.

    filename
        ``Path`` to the file to stat & hash.

    fingerprint_dir
        ``Path`` of the directory to store the stat and hash output files.
    """
    __salt__["cmd.shell"](
        "stat -c %Y {} > {}/{}.timestamp".format(
            filename, fingerprint_dir, filename.name
        )
    )
    __salt__["cmd.shell"](
        "md5sum {} > {}/{}.md5sum".format(filename, fingerprint_dir, filename.name)
    )

    # Expert plugin files get added to a conf.d dir, so keep track of the total
    # no. of files, their timestamps and content hashes
    nisysapi_conf_d_path = "/usr/lib/{0}/nisysapi/conf.d/experts/".format(
        'arm-linux-gnueabi' if 'arm' in __grains__.get('cpuarch') else 'x86_64-linux-gnu'
    )

    if os.path.exists(nisysapi_conf_d_path):
        with salt.utils.files.fopen('{0}/sysapi.conf.d.count'.format(
                NILRT_RESTARTCHECK_STATE_PATH), 'w') as fcount:
            fcount.write(str(len(os.listdir(nisysapi_conf_d_path))))

        for fexpert in os.listdir(nisysapi_conf_d_path):
            __salt__['cmd.shell']('stat -c %Y {0}/{1} >{2}/{1}.timestamp'
                                  .format(nisysapi_conf_d_path,
                                          fexpert,
                                          NILRT_RESTARTCHECK_STATE_PATH))
            __salt__['cmd.shell']('md5sum {0}/{1} >{2}/{1}.md5sum'
                                  .format(nisysapi_conf_d_path,
                                          fexpert,
                                          NILRT_RESTARTCHECK_STATE_PATH))


def _get_restartcheck_result(errors):
    """
    Return restartcheck result and append errors (if any) to ``errors``
    """
    rs_result = __salt__["restartcheck.restartcheck"](verbose=False)
    if isinstance(rs_result, dict) and "comment" in rs_result:
        errors.append(rs_result["comment"])
    return rs_result


def _process_restartcheck_result(rs_result, **kwargs):
    """
    Check restartcheck output to see if system/service restarts were requested
    and take appropriate action.
    """
    if "No packages seem to need to be restarted" in rs_result:
        return
    reboot_required = False
    for rstr in rs_result:
        if "System restart required" in rstr:
            _update_nilrt_restart_state()
            __salt__["system.set_reboot_required_witnessed"]()
            reboot_required = True
    if kwargs.get("always_restart_services", True) or not reboot_required:
        for rstr in rs_result:
            if "System restart required" not in rstr:
                service = os.path.join("/etc/init.d", rstr)
                if os.path.exists(service):
                    __salt__["cmd.run"]([service, "restart"])


def __virtual__():
    """
    Confirm this module is on a nilrt based system
    """
    if __grains__.get("os_family") == "NILinuxRT":
        try:
            os.makedirs(NILRT_RESTARTCHECK_STATE_PATH)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                return (
                    False,
                    "Error creating {} (-{}): {}".format(
                        NILRT_RESTARTCHECK_STATE_PATH, exc.errno, exc.strerror
                    ),
                )
        # populate state dir if empty
        if not os.listdir(NILRT_RESTARTCHECK_STATE_PATH):
            _update_nilrt_restart_state()
        return __virtualname__

    if os.path.isdir(OPKG_CONFDIR):
        return __virtualname__
    return False, "Module opkg only works on OpenEmbedded based systems"


def latest_version(*names, **kwargs):
    """
    Return the latest version of the named package available for upgrade or
    installation. If more than one package name is specified, a dict of
    name/version pairs is returned.

    If the latest version of a given package is already installed, an empty
    string will be returned for that package.

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.latest_version <package name>
        salt '*' pkg.latest_version <package name>
        salt '*' pkg.latest_version <package1> <package2> <package3> ...
    """
    refresh = salt.utils.data.is_true(kwargs.pop("refresh", True))

    if len(names) == 0:
        return ""

    ret = {}
    for name in names:
        ret[name] = ""

    # Refresh before looking for the latest version available
    if refresh:
        refresh_db()

    cmd = ["opkg", "list-upgradable"]
    out = __salt__["cmd.run_stdout"](cmd, output_loglevel="trace", python_shell=False)
    for line in salt.utils.itertools.split(out, "\n"):
        try:
            name, _oldversion, newversion = line.split(" - ")
            if name in names:
                ret[name] = newversion
        except ValueError:
            pass

    # Return a string if only one package name passed
    if len(names) == 1:
        return ret[names[0]]
    return ret


# available_version is being deprecated
available_version = latest_version


def version(*names, **kwargs):
    """
    Returns a string representing the package version or an empty string if not
    installed. If more than one package name is specified, a dict of
    name/version pairs is returned.

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.version <package name>
        salt '*' pkg.version <package1> <package2> <package3> ...
    """
    return __salt__["pkg_resource.version"](*names, **kwargs)


def _call_opkg(args, **kwargs):
    '''
    Call opkg utility.
    '''
    params = {
        'output_loglevel': 'trace',
        'python_shell': False,
    }
    params.update(kwargs)
    for idx in range(5):
        cmd_ret = __salt__['cmd.run_all'](args, **params)
        stderr = cmd_ret.get('stderr', '')
        if 'opkg_lock: Could not lock /run/opkg.lock' in stderr and idx < 4:
            import time
            time.sleep(2)
            continue
        return cmd_ret


def refresh_db(failhard=False, **kwargs):  # pylint: disable=unused-argument
    """
    Updates the opkg database to latest packages based upon repositories

    Returns a dict, with the keys being package databases and the values being
    the result of the update attempt. Values can be one of the following:

    - ``True``: Database updated successfully
    - ``False``: Problem updating database

    failhard
        If False, return results of failed lines as ``False`` for the package
        database that encountered the error.
        If True, raise an error with a list of the package databases that
        encountered errors.

        .. versionadded:: 2018.3.0

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.refresh_db
    """
    # Remove rtag file to keep multiple refreshes from happening in pkg states
    salt.utils.pkg.clear_rtag(__opts__)
    ret = {}
    error_repos = []
    cmd = ["opkg", "update"]
    # opkg returns a non-zero retcode when there is a failure to refresh
    # from one or more repos. Due to this, ignore the retcode.
    call = _call_opkg(cmd, ignore_retcode=True, redirect_stderr=True)

    out = call["stdout"]
    prev_line = ""
    for line in salt.utils.itertools.split(out, "\n"):
        if "Inflating" in line:
            key = line.strip().split()[1][:-1]
            ret[key] = True
        elif "Updated source" in line:
            # Use the previous line.
            key = prev_line.strip().split()[1][:-1]
            ret[key] = True
        elif "Failed to download" in line:
            key = line.strip().split()[5].split(",")[0]
            ret[key] = False
            error_repos.append(key)
        prev_line = line

    if failhard and error_repos:
        raise CommandExecutionError(
            "Error getting repos: {}".format(", ".join(error_repos))
        )

    # On a non-zero exit code where no failed repos were found, raise an
    # exception because this appears to be a different kind of error.
    if call["retcode"] != 0 and not error_repos:
        raise CommandExecutionError(out)

    return ret


def _is_testmode(**kwargs):
    """
    Returns whether a test mode (noaction) operation was requested.
    """
    return bool(kwargs.get("test") or __opts__.get("test"))


def _append_noaction_if_testmode(cmd, **kwargs):
    """
    Adds the --noaction flag to the command if it's running in the test mode.
    """
    if _is_testmode(**kwargs):
        cmd.append("--noaction")


def _build_install_command_list(cmd_prefix, to_install, to_downgrade, to_reinstall):
    """
    Builds a list of install commands to be executed in sequence in order to process
    each of the to_install, to_downgrade, and to_reinstall lists.
    """
    cmds = []
    if to_install:
        cmd = copy.deepcopy(cmd_prefix)
        cmd.extend(to_install)
        cmds.append(cmd)
    if to_downgrade:
        cmd = copy.deepcopy(cmd_prefix)
        cmd.append("--force-downgrade")
        cmd.extend(to_downgrade)
        cmds.append(cmd)
    if to_reinstall:
        cmd = copy.deepcopy(cmd_prefix)
        cmd.append("--force-reinstall")
        cmd.extend(to_reinstall)
        cmds.append(cmd)

    return cmds


def _parse_reported_packages_from_install_output(output):
    """
    Parses the output of "opkg install" to determine what packages would have been
    installed by an operation run with the --noaction flag.

    We are looking for lines like:
        Installing <package> (<version>) on <target>
    or
        Upgrading <package> from <oldVersion> to <version> on <target>
    or
        Upgrading <oldPackage> (<oldVersion>) to <package> (<version>) on <target>
    """
    reported_pkgs = {}
    install_pattern = re.compile(
        r"Installing\s(?P<package>.*?)\s\((?P<version>.*?)\)\son\s(?P<target>.*?)"
    )
    internal_solver_upgrade_pattern = re.compile(
        r"Upgrading\s(?P<package>.*?)\sfrom\s(?P<oldVersion>.*?)\sto\s(?P<version>.*?)\son\s(?P<target>.*?)"
    )
    libsolv_solver_upgrade_pattern = re.compile(
        r"Upgrading\s(?P<oldPackage>.*?)\s\((?P<oldVersion>.*?)\)\sto\s(?P<package>.*?)\s\((?P<version>.*?)\)\son\s(?P<target>.*?)"
    )
    for line in salt.utils.itertools.split(output, "\n"):
        match = install_pattern.match(line)
        if match is None:
            match = internal_solver_upgrade_pattern.match(line)
        if match is None:
            match = libsolv_solver_upgrade_pattern.match(line)
        if match:
            reported_pkgs[match.group('package')] = match.group('version')

    return reported_pkgs

def _get_total_packages(cmd):
    """
    Runs the given command in test-mode (--noaction) and returns the number of packages
    that have to be installed/removed/upgraded/downgraded
    """
    test_cmd = copy.deepcopy(cmd)
    test_cmd.append('--noaction')

    out = _call_opkg(test_cmd)
    if out['retcode'] != 0:
        return -1

    output_lines = out['stdout'].split('\n')
    return sum(_get_operation_from_output_line(line, False) is not None for line in output_lines)

def _get_operation_from_output_line(line, include_download = True):
    """
    Gets the current opkg operation from output
    Example:
        'Installing vim (17.0.0) on root'
    Returns: 'install'
    """
    operations = {
            'Installing': 'install',
            'Upgrading': 'upgrade',
            'Removing': 'remove',
            'Downgrading': 'downgrade'
            }
    if include_download:
        operations['Downloading'] = 'download'

    line_tokens = line.split()

    return operations.get(line_tokens[0]) if line_tokens else None


def _get_package_from_output_line(operation, line):
    """
    Gets the package that is currently Installing/Upgrading/Removing/Downgrading
    Returns the name of the package that opkg is currently processing

    Example:
        operation = 'install'
        line = 'Installing vim (17.0.0) on root'
    Returns: vim
    """
    package = None
    if operation == 'download' and line.endswith('.ipk.\n'):
        # Example: 'Downloading http://nickdanger.amer.corp.natinst.com/feeds/OneRT/20.0/x64//ni-xnet-notices_20.0.0.49152-0+f0_all.ipk.'
        package = line.split('/')[-1].split('_')[0]
    elif operation in ['install', 'upgrade', 'remove', 'downgrade']:
        line_tokens = line.split()
        # For example: "Installing/Removing/.. package_name (version_number) ..."
        # We do not want to count "Removing any package that.." messages
        if len(line_tokens) >= 3 and line_tokens[2].startswith('('):
            package = line_tokens[1]
    
    return package


def _get_operation_and_package_from_output_line(line):
    """
    Returns the (operation, package-name) combination if the line is a valid opkg operation output
    If operation or package-name could not be parsed from the line, it returns None instead of the
    operation/package-name
    """
    operation = _get_operation_from_output_line(line)
    current_package = _get_package_from_output_line(operation, line)

    return operation, current_package


def _process_with_progress(cmd, jid, total_packages_count):
    """
    Process the opkg command and fire events to the salt-master bus
    indicating the progress of the operation
    """
    stdout = ''
    minion_id = str(salt.config.get_id(__opts__)[0])
    notify_progress_period = __opts__.get('pkg_progress_period', 60)
    last_notify_timestamp = 0
    processed_count = 0
    force_update = True
    last_sent_package = None
    current_package = None
    operation = None
    event = salt.utils.event.get_event(
            'minion', opts=__opts__, listen=False
            )
    tag = 'salt/minion/pkg_progress/{0}'.format(minion_id)
    proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            universal_newlines=True
            )
    # We don't want the standard output to be blocking to avoid missing updates
    # based on specified progress period
    try:
        flags = fcntl(proc.stdout, F_GETFL)
        stdout_fileno = proc.stdout.fileno()
        fcntl(stdout_fileno, F_SETFL, flags | os.O_NONBLOCK)
        while proc.poll() is None:
            ready_to_read = select.select([stdout_fileno], [], [])[0]
            if not ready_to_read:
                time.sleep(1)
            else:
                output = proc.stdout.readline()
                stdout += output
                line_operation, line_package = _get_operation_and_package_from_output_line(output)
                if line_operation and line_package:
                    operation = line_operation
                    current_package = line_package
                    if operation in ['install', 'upgrade', 'remove', 'downgrade']:
                        processed_count += 1
                        force_update |= processed_count in [1, total_packages_count]
            timestamp = time.time()
            if (timestamp - last_notify_timestamp > notify_progress_period or force_update) and \
                    last_sent_package != current_package:
                force_update = False
                last_sent_package = current_package
                last_notify_timestamp = timestamp
                data = {
                        'jid': jid,
                        'progress_info': {
                            'operation': operation,
                            'package': current_package,
                            'count': processed_count,
                            'total_count': total_packages_count
                        }
                }
                event.fire_master(data, tag)
    except Exception as exc:
        log.warning('Could not get progress due to the following exception: "%s"', str(exc))
    output, stderr = proc.communicate()

    return {
        'retcode': proc.returncode,
        'stdout': stdout + output,
        'stderr': stderr
    }

def _execute_install_command(cmd, parse_output, errors, parsed_packages, jid, should_process_in_detached_mode):
    """
    Executes a command for the install operation.
    If the command fails, its error output will be appended to the errors list.
    If the command succeeds and parse_output is true, updated packages will be appended
    to the parsed_packages dictionary.
    """
    out = {}
    if __opts__.get('notify_pkg_progress') and not parse_output and not should_process_in_detached_mode:
        total_packages_count = _get_total_packages(cmd)
        out = _process_with_progress(cmd, jid, total_packages_count) if total_packages_count != -1 else _call_opkg(cmd)
    else:
        if should_process_in_detached_mode and not parse_output:
            proc = _open_detached_process(cmd)
            # If the process doesn't complete in 24 hours, standard systemlink install timeout,
            # the communicate operation will raise a TimeoutExpired. We will let salt report it.
            stdout, stderr = proc.communicate(timeout=86400)  # pylint: disable=unexpected-keyword-arg
            out = {}
            out['retcode'] = proc.returncode
            if out['retcode'] != 0:
                out['stderr'] = 'Opkg installation operation failed with: {}'.format(out['retcode'])
        else:
            out = _call_opkg(cmd)

    if out['retcode'] != 0:
        if out['stderr']:
            errors.append(out['stderr'])
        else:
            errors.append(out['stdout'])
    elif parse_output:
        parsed_packages.update(_parse_reported_packages_from_install_output(out['stdout']))


def _open_detached_process(cmd):
    '''
    Create a process that is not a child of the current process,
    otherwise known as a detached process.
    '''
    kwargs = {'start_new_session': True}
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs
    )
    return proc


def install(
    name=None, refresh=False, pkgs=None, sources=None, reinstall=False, **kwargs
):
    """
    Install the passed package, add refresh=True to update the opkg database.

    name
        The name of the package to be installed. Note that this parameter is
        ignored if either "pkgs" or "sources" is passed. Additionally, please
        note that this option can only be used to install packages from a
        software repository. To install a package file manually, use the
        "sources" option.

        CLI Example:

        .. code-block:: bash

            salt '*' pkg.install <package name>

    refresh
        Whether or not to refresh the package database before installing.

    version
        Install a specific version of the package, e.g. 1.2.3~0ubuntu0. Ignored
        if "pkgs" or "sources" is passed.

        .. versionadded:: 2017.7.0

    reinstall : False
        Specifying reinstall=True will use ``opkg install --force-reinstall``
        rather than simply ``opkg install`` for requested packages that are
        already installed.

        If a version is specified with the requested package, then ``opkg
        install --force-reinstall`` will only be used if the installed version
        matches the requested version.

        .. versionadded:: 2017.7.0


    Multiple Package Installation Options:

    pkgs
        A list of packages to install from a software repository. Must be
        passed as a python list.

        CLI Example:

        .. code-block:: bash

            salt '*' pkg.install pkgs='["foo", "bar"]'
            salt '*' pkg.install pkgs='["foo", {"bar": "1.2.3-0ubuntu0"}]'

    sources
        A list of IPK packages to install. Must be passed as a list of dicts,
        with the keys being package names, and the values being the source URI
        or local path to the package.  Dependencies are automatically resolved
        and marked as auto-installed.

        CLI Example:

        .. code-block:: bash

            salt '*' pkg.install sources='[{"foo": "salt://foo.deb"},{"bar": "salt://bar.deb"}]'

    install_recommends
        Whether to install the packages marked as recommended. Default is True.

    only_upgrade
        Only upgrade the packages (disallow downgrades), if they are already
        installed. Default is False.

        .. versionadded:: 2017.7.0

    always_restart_services
        Whether to restart services even if a reboot is required. Default is True.

    Returns a dict containing the new package names and versions::

        {'<package>': {'old': '<old-version>',
                       'new': '<new-version>'}}
    """
    refreshdb = salt.utils.data.is_true(refresh)

    try:
        pkg_params, pkg_type = __salt__["pkg_resource.parse_targets"](
            name, pkgs, sources, **kwargs
        )
    except MinionError as exc:
        raise CommandExecutionError(exc)

    list_pkgs_errors = []
    old = _execute_list_pkgs(list_pkgs_errors, False)
    if list_pkgs_errors:
        raise CommandExecutionError(
            "Problem encountered before installing package(s)",
            info={"errors": list_pkgs_errors}
        )

    cmd_prefix = ["opkg", "install"]
    to_install = []
    to_install_detached = []
    to_reinstall = []
    to_downgrade = []

    _append_noaction_if_testmode(cmd_prefix, **kwargs)
    if pkg_params is None or len(pkg_params) == 0:
        return {}
    elif pkg_type == "file":
        if reinstall:
            cmd_prefix.append("--force-reinstall")
        if not kwargs.get("only_upgrade", False):
            cmd_prefix.append("--force-downgrade")
        to_install.extend(pkg_params)
    elif pkg_type == "repository":
        if not kwargs.get("install_recommends", True):
            cmd_prefix.append("--no-install-recommends")
        for pkgname, pkgversion in pkg_params.items():
            if name and pkgs is None and kwargs.get("version") and len(pkg_params) == 1:
                # Only use the 'version' param if 'name' was not specified as a
                # comma-separated list
                version_num = kwargs["version"]
            else:
                version_num = pkgversion

            if version_num is None:
                # Don't allow downgrades if the version
                # number is not specified.
                if reinstall and pkgname in old:
                    to_reinstall.append(pkgname)
                else:
                    to_install.append(pkgname)
            else:
                cver = old.get(pkgname, '')
                version_conditions = [x.strip() for x in version_num.split(',')]
                for version_condition in version_conditions:
                    (version_string, version_operator, operator_specified) = _get_version_info(version_condition)
                    if operator_specified:
                        # Version conditions are sent to the solver as install commands
                        pkgstr = '{0}{1}{2}'.format(pkgname, version_operator, version_string)
                        _handle_to_install_package(pkgstr, pkgname, to_install, to_install_detached)
                    else:
                        pkgstr = "{}={}".format(pkgname, version_num)
                        if (
                            reinstall
                            and cver
                            and salt.utils.versions.compare(
                                ver1=version_num, oper="==", ver2=cver, cmp_func=version_cmp
                            )
                        ):
                            to_reinstall.append(pkgstr)
                        elif not cver or salt.utils.versions.compare(
                            ver1=version_num, oper=">=", ver2=cver, cmp_func=version_cmp
                        ):
                            _handle_to_install_package(pkgstr, pkgname, to_install, to_install_detached)
                        else:
                            if not kwargs.get("only_upgrade", False):
                                to_downgrade.append(pkgstr)
                            else:
                                # This should cause the command to fail.
                                _handle_to_install_package(pkgstr, pkgname, to_install, to_install_detached)

    cmds = _build_install_command_list(
        cmd_prefix, to_install, to_downgrade, to_reinstall
    )
    detached_cmds = _build_install_command_list(
        cmd_prefix, to_install_detached, None, None
    )

    if not cmds and not detached_cmds:
        return {}

    feeds_updated_status = {}
    if refreshdb:
        feeds_updated_status = refresh_db()
    failed_to_update_feeds = [feed for feed in feeds_updated_status if not feeds_updated_status[feed]]
    feed_update_error = None
    if failed_to_update_feeds:
        feed_update_error = 'Error getting repos: {0}'.format(', '.join(failed_to_update_feeds))
    errors = []
    is_testmode = _is_testmode(**kwargs)
    test_packages = {}
    for cmd in cmds:
        _execute_install_command(cmd, is_testmode, errors, test_packages)

    __context__.pop("pkg.list_pkgs", None)
    new = list_pkgs()
    if is_testmode:
        new = copy.deepcopy(new)
        new.update(test_packages)

    ret = salt.utils.data.compare_dicts(old, new)

    if pkg_type == "file" and reinstall:
        # For file-based packages, prepare 'to_reinstall' to have a list
        # of all the package names that may have been reinstalled.
        # This way, we could include reinstalled packages in 'ret'.
        for pkgfile in to_install:
            # Convert from file name to package name.
            cmd = ["opkg", "info", pkgfile]
            out = __salt__["cmd.run_all"](
                cmd, output_loglevel="trace", python_shell=False
            )
            if out["retcode"] == 0:
                # Just need the package name.
                pkginfo_dict = _process_info_installed_output(out["stdout"], [])
                if pkginfo_dict:
                    to_reinstall.append(next(iter(pkginfo_dict)))

    for pkgname in to_reinstall:
        if pkgname not in ret or pkgname in old:
            ret.update(
                {pkgname: {"old": old.get(pkgname, ""), "new": new.get(pkgname, "")}}
            )

    rs_result = _get_restartcheck_result(errors)

    if errors:
        if feed_update_error:
            errors.append(feed_update_error)
        raise CommandExecutionError(
            "Problem encountered installing package(s)",
            info={"errors": errors, "changes": ret},
        )

    _process_restartcheck_result(rs_result, **kwargs)

    if list_pkgs_errors:
        raise CommandExecutionError(
            'Problem encountered after successfully installing package(s). Cannot provide changes list.',
            info={'errors': list_pkgs_errors}
        )

    return ret

def _handle_to_install_package(pkg_string, pkg_name, to_install, to_install_detached):
    '''
    Check whether the package should be installed by a detached process or not
    :param pkg_string: Full package string containing the name and the version
    :param pkg_name: Package name
    :param to_install: List of packages to be installed normally
    :param to_install_detached: List of packages to be installed detached
    :return:
    '''
    if pkg_name in PACKAGES_TO_INSTALL_DETACHED:
        to_install_detached.append(pkg_string)
    else:
        to_install.append(pkg_string)


def _get_version_info(version_string):
    '''
    Detects if the version has comparison operators.
    Supported version operators are: >>, <<, >=, <=, !=
    Returns:
        Tuple: Containing versionString, operatorString, operatorSpecified
    '''
    versionString = version_string
    operatorString = ''
    operatorSpecified = False
    versionRegex = r'(<=>|!=|>=|<=|>>|<<|<>|>|<|=)\s?((?:[0-9]+:)?[0-9][a-zA-Z0-9+~.-]*)'
    match = re.search(versionRegex, version_string)
    if match:
        operatorSpecified = True
        versionString = match.group(2)
        operatorString = match.group(1)
    return (versionString, operatorString, operatorSpecified)


def _parse_reported_packages_from_remove_output(output):
    '''
    Parses the output of "opkg remove" to determine what packages would have been
    removed by an operation run with the --noaction flag.

    We are looking for lines like
        Removing <package> (<version>) from <Target>...
    '''
    reported_pkgs = {}
    remove_pattern = re.compile(r'Removing\s(?P<package>.*?)\s\((?P<version>.*?)\)\sfrom\s(?P<target>.*?)...')
    for line in salt.utils.itertools.split(output, '\n'):
        match = remove_pattern.match(line)
        if match:
            reported_pkgs[match.group('package')] = ''

    return reported_pkgs

def _execute_remove_command(cmd, is_testmode, errors, reportedPkgs, jid):
    '''
    Executes a command for the remove operation.
    If the command fails, its error output will be set to the errors list.
    '''
    out = {}
    if __opts__.get('notify_pkg_progress') and not is_testmode:
        total_packages_count = _get_total_packages(cmd)
        out = _process_with_progress(cmd, jid, total_packages_count) if total_packages_count != -1 else _call_opkg(cmd)
    else:
        out = _call_opkg(cmd)

    if out['retcode'] != 0:
        if out['stderr']:
            errors.append(out['stderr'])
        else:
            errors.append(out['stdout'])
    elif is_testmode:
        reportedPkgs.update(_parse_reported_packages_from_remove_output(out['stdout']))

def _parse_reported_packages_from_remove_output(output):
    """
    Parses the output of "opkg remove" to determine what packages would have been
    removed by an operation run with the --noaction flag.

    We are looking for lines like
        Removing <package> (<version>) from <Target>...
    """
    reported_pkgs = {}
    remove_pattern = re.compile(
        r"Removing\s(?P<package>.*?)\s\((?P<version>.*?)\)\sfrom\s(?P<target>.*?)..."
    )
    for line in salt.utils.itertools.split(output, "\n"):
        match = remove_pattern.match(line)
        if match:
            reported_pkgs[match.group("package")] = ""

    return reported_pkgs


def remove(name=None, pkgs=None, **kwargs):  # pylint: disable=unused-argument
    """
    Remove packages using ``opkg remove``.

    name
        The name of the package to be deleted.


    Multiple Package Options:

    pkgs
        A list of packages to delete. Must be passed as a python list. The
        ``name`` parameter will be ignored if this option is passed.

    remove_dependencies
        Remove package and all dependencies

        .. versionadded:: 2019.2.0

    auto_remove_deps
        Remove packages that were installed automatically to satisfy dependencies

        .. versionadded:: 2019.2.0

    Returns a dict containing the changes.

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.remove <package name>
        salt '*' pkg.remove <package1>,<package2>,<package3>
        salt '*' pkg.remove pkgs='["foo", "bar"]'
        salt '*' pkg.remove pkgs='["foo", "bar"]' remove_dependencies=True auto_remove_deps=True
    """
    try:
        pkg_params = __salt__["pkg_resource.parse_targets"](name, pkgs)[0]
    except MinionError as exc:
        raise CommandExecutionError(exc)

    old = info_installed(attr="version")
    old = {key: value.get('version') for key,value in old.items()}

    targets = [x for x in pkg_params if x in old]
    if not targets:
        return {}
    cmd = ["opkg", "remove"]
    _append_noaction_if_testmode(cmd, **kwargs)
    if kwargs.get("remove_dependencies", False):
        cmd.append("--force-removal-of-dependent-packages")
    if kwargs.get("auto_remove_deps", False):
        cmd.append("--autoremove")
    cmd.extend(targets)

    errors = []
    jid = kwargs.get("__pub_jid")
    is_testmode = _is_testmode(**kwargs)
    reportedPkgs = {}
    _execute_remove_command(cmd, is_testmode, errors, reportedPkgs, jid)

    __context__.pop("pkg.list_pkgs", None)
    new = info_installed(attr="version")
    new = {key: value.get("version") for key,value in new.items()}
    if is_testmode:
        new = {k: v for k, v in new.items() if k not in reportedPkgs}

    ret = salt.utils.data.compare_dicts(old, new)

    rs_result = _get_restartcheck_result(errors)

    if errors:
        raise CommandExecutionError(
            "Problem encountered removing package(s)",
            info={"errors": errors, "changes": ret},
        )

    _process_restartcheck_result(rs_result, **kwargs)

    return ret


def purge(name=None, pkgs=None, **kwargs):  # pylint: disable=unused-argument
    """
    Package purges are not supported by opkg, this function is identical to
    :mod:`pkg.remove <salt.modules.opkg.remove>`.

    name
        The name of the package to be deleted.


    Multiple Package Options:

    pkgs
        A list of packages to delete. Must be passed as a python list. The
        ``name`` parameter will be ignored if this option is passed.


    Returns a dict containing the changes.

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.purge <package name>
        salt '*' pkg.purge <package1>,<package2>,<package3>
        salt '*' pkg.purge pkgs='["foo", "bar"]'
    """
    return remove(name=name, pkgs=pkgs)


def upgrade(refresh=True, **kwargs):  # pylint: disable=unused-argument
    """
    Upgrades all packages via ``opkg upgrade``

    Returns a dictionary containing the changes:

    .. code-block:: python

        {'<package>':  {'old': '<old-version>',
                        'new': '<new-version>'}}

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.upgrade
    """
    ret = {
        "changes": {},
        "result": True,
        "comment": "",
    }

    errors = []

    if salt.utils.data.is_true(refresh):
        refresh_db()

    list_pkgs_errors = []
    old = _execute_list_pkgs(list_pkgs_errors, False)
    if list_pkgs_errors:
        raise CommandExecutionError(
            'Problem encountered before upgrading package(s)',
            info={'errors': list_pkgs_errors}
        )

    cmd = ["opkg", "upgrade"]
    result = _call_opkg(cmd)
    __context__.pop("pkg.list_pkgs", None)
    new = _execute_list_pkgs(list_pkgs_errors, False)
    if not list_pkgs_errors:
        ret = salt.utils.data.compare_dicts(old, new)

    if result["retcode"] != 0:
        errors.append(result)

    rs_result = _get_restartcheck_result(errors)

    if errors:
        raise CommandExecutionError(
            "Problem encountered upgrading packages",
            info={"errors": errors, "changes": ret},
        )

    _process_restartcheck_result(rs_result, **kwargs)

    if list_pkgs_errors:
        raise CommandExecutionError(
            'Problem encountered after successfully upgrading package(s). Cannot provide changes list.',
            info={'errors': list_pkgs_errors}
        )

    return ret


def hold(name=None, pkgs=None, sources=None, **kwargs):  # pylint: disable=W0613
    """
    Set package in 'hold' state, meaning it will not be upgraded.

    name
        The name of the package, e.g., 'tmux'

        CLI Example:

        .. code-block:: bash

            salt '*' pkg.hold <package name>

    pkgs
        A list of packages to hold. Must be passed as a python list.

        CLI Example:

        .. code-block:: bash

            salt '*' pkg.hold pkgs='["foo", "bar"]'
    """
    if not name and not pkgs and not sources:
        raise SaltInvocationError("One of name, pkgs, or sources must be specified.")
    if pkgs and sources:
        raise SaltInvocationError("Only one of pkgs or sources can be specified.")

    targets = []
    if pkgs:
        targets.extend(pkgs)
    elif sources:
        for source in sources:
            targets.append(next(iter(source)))
    else:
        targets.append(name)

    ret = {}
    for target in targets:
        if isinstance(target, dict):
            target = next(iter(target))

        ret[target] = {"name": target, "changes": {}, "result": False, "comment": ""}

        state = _get_state(target)
        if not state:
            ret[target]["comment"] = "Package {} not currently held.".format(target)
        elif state != "hold":
            if "test" in __opts__ and __opts__["test"]:
                ret[target].update(result=None)
                ret[target]["comment"] = "Package {} is set to be held.".format(target)
            else:
                result = _set_state(target, "hold")
                ret[target].update(changes=result[target], result=True)
                ret[target]["comment"] = "Package {} is now being held.".format(target)
        else:
            ret[target].update(result=True)
            ret[target]["comment"] = "Package {} is already set to be held.".format(
                target
            )
    return ret


def unhold(name=None, pkgs=None, sources=None, **kwargs):  # pylint: disable=W0613
    """
    Set package current in 'hold' state to install state,
    meaning it will be upgraded.

    name
        The name of the package, e.g., 'tmux'

        CLI Example:

        .. code-block:: bash

            salt '*' pkg.unhold <package name>

    pkgs
        A list of packages to hold. Must be passed as a python list.

        CLI Example:

        .. code-block:: bash

            salt '*' pkg.unhold pkgs='["foo", "bar"]'
    """
    if not name and not pkgs and not sources:
        raise SaltInvocationError("One of name, pkgs, or sources must be specified.")
    if pkgs and sources:
        raise SaltInvocationError("Only one of pkgs or sources can be specified.")

    targets = []
    if pkgs:
        targets.extend(pkgs)
    elif sources:
        for source in sources:
            targets.append(next(iter(source)))
    else:
        targets.append(name)

    ret = {}
    for target in targets:
        if isinstance(target, dict):
            target = next(iter(target))

        ret[target] = {"name": target, "changes": {}, "result": False, "comment": ""}

        state = _get_state(target)
        if not state:
            ret[target]["comment"] = "Package {} does not have a state.".format(target)
        elif state == "hold":
            if "test" in __opts__ and __opts__["test"]:
                ret[target].update(result=None)
                ret["comment"] = "Package {} is set not to be held.".format(target)
            else:
                result = _set_state(target, "ok")
                ret[target].update(changes=result[target], result=True)
                ret[target][
                    "comment"
                ] = "Package {} is no longer being " "held.".format(target)
        else:
            ret[target].update(result=True)
            ret[target][
                "comment"
            ] = "Package {} is already set not to be " "held.".format(target)
    return ret


def _get_state(pkg):
    """
    View package state from the opkg database

    Return the state of pkg
    """
    cmd = ["opkg", "status"]
    cmd.append(pkg)
    out = __salt__["cmd.run"](cmd, python_shell=False)
    state_flag = ""
    for line in salt.utils.itertools.split(out, "\n"):
        if line.startswith("Status"):
            _status, _state_want, state_flag, _state_status = line.split()

    return state_flag


def _set_state(pkg, state):
    """
    Change package state on the opkg database

    The state can be any of:

     - hold
     - noprune
     - user
     - ok
     - installed
     - unpacked

    This command is commonly used to mark a specific package to be held from
    being upgraded, that is, to be kept at a certain version.

    Returns a dict containing the package name, and the new and old
    versions.
    """
    ret = {}
    valid_states = ("hold", "noprune", "user", "ok", "installed", "unpacked")
    if state not in valid_states:
        raise SaltInvocationError("Invalid state: {}".format(state))
    oldstate = _get_state(pkg)
    cmd = ["opkg", "flag"]
    cmd.append(state)
    cmd.append(pkg)
    _out = __salt__["cmd.run"](cmd, python_shell=False)

    # Missing return value check due to opkg issue 160
    ret[pkg] = {"old": oldstate, "new": state}
    return ret


def _list_pkgs_from_context(versions_as_list):
    """
    Use pkg list from __context__
    """
    if versions_as_list:
        return __context__["pkg.list_pkgs"]
    else:
        ret = copy.deepcopy(__context__["pkg.list_pkgs"])
        __salt__["pkg_resource.stringify"](ret)
        return ret


def list_pkgs(versions_as_list=False, **kwargs):
    """
    List the packages currently installed in a dict::

        {'<package_name>': '<version>'}

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.list_pkgs
        salt '*' pkg.list_pkgs versions_as_list=True
    """
    errors = []
    ret = _execute_list_pkgs(errors, versions_as_list, **kwargs)

    if errors:
        raise CommandExecutionError(
            'Problem encountered listing package(s)',
            info={'errors': errors}
        )
    return ret


def _execute_list_pkgs(errors, versions_as_list=False, **kwargs):
    """
    List the packages currently installed in a dict::

        {'<package_name>': '<version>'}

    Accumulates errors in errors variable
    """
    versions_as_list = salt.utils.data.is_true(versions_as_list)
    # not yet implemented or not applicable
    if any(
        [salt.utils.data.is_true(kwargs.get(x)) for x in ("removed", "purge_desired")]
    ):
        return {}

    if "pkg.list_pkgs" in __context__:
        return _list_pkgs_from_context(versions_as_list)

    cmd = ["opkg", "list-installed"]
    ret = {}
    out_dict = _call_opkg(cmd)

    if out_dict["retcode"] != 0:
        if out_dict["stderr"]:
            errors.append(out_dict["stderr"])
        else:
            errors.append([out_dict["stdout"]])
        return ret

    out = out_dict["stdout"]
    for line in salt.utils.itertools.split(out, "\n"):
        # This is a continuation of package description
        if not line or line[0] == " ":
            continue

        # This contains package name, version, and description.
        # Extract the first two.
        pkg_name, pkg_version = line.split(" - ", 2)[:2]
        __salt__["pkg_resource.add_pkg"](ret, pkg_name, pkg_version)

    __salt__["pkg_resource.sort_pkglist"](ret)
    __context__["pkg.list_pkgs"] = copy.deepcopy(ret)
    if not versions_as_list:
        __salt__["pkg_resource.stringify"](ret)
    return ret


def list_upgrades(refresh=True, **kwargs):  # pylint: disable=unused-argument
    """
    List all available package upgrades.

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.list_upgrades
    """
    ret = {}
    if salt.utils.data.is_true(refresh):
        refresh_db()

    cmd = ["opkg", "list-upgradable"]
    call = _call_opkg(cmd)

    if call["retcode"] != 0:
        comment = ""
        if "stderr" in call:
            comment += call["stderr"]
        if "stdout" in call:
            comment += call["stdout"]
        raise CommandExecutionError(comment)
    else:
        out = call["stdout"]

    for line in out.splitlines():
        name, _oldversion, newversion = line.split(" - ")
        ret[name] = newversion

    return ret


def _convert_to_standard_attr(attr):
    """
    Helper function for _process_info_installed_output()

    Converts an opkg attribute name to a standard attribute
    name which is used across 'pkg' modules.
    """
    ret_attr = ATTR_MAP.get(attr, None)
    if ret_attr is None:
        # All others convert to lowercase
        return attr.lower()
    return ret_attr


def _process_info_installed_output(out, filter_attrs):
    """
    Helper function for info_installed()

    Processes stdout output from a single invocation of
    'opkg status'.
    """
    ret = {}
    name = None
    attrs = {}
    attr = None

    for line in salt.utils.itertools.split(out, "\n"):
        if line and line[0] == " ":
            # This is a continuation of the last attr
            if filter_attrs is None or attr in filter_attrs:
                line = line.strip()
                if attrs[attr]:
                    # If attr is empty, don't add leading newline
                    attrs[attr] += "\n"
                attrs[attr] += line
            continue
        line = line.strip()
        if not line:
            # Separator between different packages
            if name:
                ret[name] = attrs
            name = None
            attrs = {}
            attr = None
            continue
        key, value = line.split(":", 1)
        value = value.lstrip()
        attr = _convert_to_standard_attr(key)
        if attr == "name":
            name = value
        elif filter_attrs is None or attr in filter_attrs:
            attrs[attr] = value

    if name:
        ret[name] = attrs
    return ret


def info_installed(*names, **kwargs):
    """
    Return the information of the named package(s), installed on the system.

    .. versionadded:: 2017.7.0

    :param names:
        Names of the packages to get information about. If none are specified,
        will return information for all installed packages.

    :param attr:
        Comma-separated package attributes. If no 'attr' is specified, all available attributes returned.

        Valid attributes are:
            arch, conffiles, conflicts, depends, description, filename, group,
            install_date_time_t, md5sum, packager, provides, recommends,
            replaces, size, source, suggests, url, version

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.info_installed
        salt '*' pkg.info_installed attr=version,packager
        salt '*' pkg.info_installed <package1>
        salt '*' pkg.info_installed <package1> <package2> <package3> ...
        salt '*' pkg.info_installed <package1> attr=version,packager
        salt '*' pkg.info_installed <package1> <package2> <package3> ... attr=version,packager
    """
    attr = kwargs.pop("attr", None)
    if attr is None:
        filter_attrs = None
    elif isinstance(attr, str):
        filter_attrs = set(attr.split(","))
    else:
        filter_attrs = set(attr)

    ret = {}
    if names:
        # Specific list of names of installed packages
        for name in names:
            cmd = ["opkg", "status", name]
            call = _call_opkg(cmd)
            if call["retcode"] != 0:
                comment = ""
                if call["stderr"]:
                    comment += call["stderr"]
                else:
                    comment += call["stdout"]

                raise CommandExecutionError(comment)
            ret.update(_process_info_installed_output(call["stdout"], filter_attrs))
    else:
        # All installed packages
        cmd = ["opkg", "status"]
        call = _call_opkg(cmd)
        if call["retcode"] != 0:
            comment = ""
            if call["stderr"]:
                comment += call["stderr"]
            else:
                comment += call["stdout"]

            raise CommandExecutionError(comment)
        ret.update(_process_info_installed_output(call["stdout"], filter_attrs))

    return ret


def upgrade_available(name, **kwargs):  # pylint: disable=unused-argument
    """
    Check whether or not an upgrade is available for a given package

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.upgrade_available <package name>
    """
    return latest_version(name) != ""


def version_cmp(
    pkg1, pkg2, ignore_epoch=False, **kwargs
):  # pylint: disable=unused-argument
    """
    Do a cmp-style comparison on two packages. Return -1 if pkg1 < pkg2, 0 if
    pkg1 == pkg2, and 1 if pkg1 > pkg2. Return None if there was a problem
    making the comparison.

    ignore_epoch : False
        Set to ``True`` to ignore the epoch when comparing versions

        .. versionadded:: 2016.3.4

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.version_cmp '0.2.4-0' '0.2.4.1-0'
    """
    normalize = lambda x: str(x).split(":", 1)[-1] if ignore_epoch else str(x)
    pkg1 = normalize(pkg1)
    pkg2 = normalize(pkg2)

    output = __salt__["cmd.run_stdout"](
        ["opkg", "--version"], output_loglevel="trace", python_shell=False
    )
    opkg_version = output.split(" ")[2].strip()
    if salt.utils.versions.LooseVersion(
        opkg_version
    ) >= salt.utils.versions.LooseVersion("0.3.4"):
        cmd_compare = ["opkg", "compare-versions"]
    elif salt.utils.path.which("opkg-compare-versions"):
        cmd_compare = ["opkg-compare-versions"]
    else:
        log.warning(
            "Unable to find a compare-versions utility installed. Either upgrade opkg to "
            "version > 0.3.4 (preferred) or install the older opkg-compare-versions script."
        )
        return None

    for oper, ret in (("<<", -1), ("=", 0), (">>", 1)):
        cmd = cmd_compare[:]
        cmd.append(shlex.quote(pkg1))
        cmd.append(oper)
        cmd.append(shlex.quote(pkg2))
        retcode = __salt__["cmd.retcode"](
            cmd, output_loglevel="trace", ignore_retcode=True, python_shell=False
        )
        if retcode == 0:
            return ret
    return None


def _set_repo_option(repo, option):
    """
    Set the option to repo
    """
    if not option:
        return
    opt = option.split("=")
    if len(opt) != 2:
        return
    if opt[0] == "trusted":
        repo["trusted"] = opt[1] == "yes"
    else:
        repo[opt[0]] = opt[1]


def _set_repo_options(repo, options):
    """
    Set the options to the repo.
    """
    delimiters = "[", "]"
    pattern = "|".join(map(re.escape, delimiters))
    for option in options:
        splitted = re.split(pattern, option)
        for opt in splitted:
            _set_repo_option(repo, opt)


def _create_repo(line, filename):
    """
    Create repo
    """
    repo = {}
    if line.startswith("#"):
        repo["enabled"] = False
        line = line[1:]
    else:
        repo["enabled"] = True
    cols = salt.utils.args.shlex_split(line.strip())
    repo["compressed"] = not cols[0] in "src"
    repo["name"] = cols[1]
    repo["uri"] = cols[2]
    repo["file"] = os.path.join(OPKG_CONFDIR, filename)
    if len(cols) > 3:
        _set_repo_options(repo, cols[3:])
    return repo


def _read_repos(conf_file, repos, filename, regex):
    """
    Read repos from configuration file
    """
    for line in conf_file:
        line = salt.utils.stringutils.to_unicode(line)
        if not regex.search(line):
            continue
        repo = _create_repo(line, filename)

        # do not store duplicated uri's
        if repo["uri"] not in repos:
            repos[repo["uri"]] = [repo]


def list_repos(**kwargs):  # pylint: disable=unused-argument
    """
    Lists all repos on ``/etc/opkg/*.conf``

    CLI Example:

    .. code-block:: bash

       salt '*' pkg.list_repos
    """
    repos = {}
    regex = re.compile(REPO_REGEXP)
    for filename in os.listdir(OPKG_CONFDIR):
        if not filename.endswith(".conf"):
            continue
        with salt.utils.files.fopen(os.path.join(OPKG_CONFDIR, filename)) as conf_file:
            _read_repos(conf_file, repos, filename, regex)
    return repos


def get_repo(repo, **kwargs):  # pylint: disable=unused-argument
    """
    Display a repo from the ``/etc/opkg/*.conf``

    CLI Examples:

    .. code-block:: bash

        salt '*' pkg.get_repo repo
    """
    repos = list_repos()

    if repos:
        for source in repos.values():
            for sub in source:
                if sub["name"] == repo:
                    return sub
    return {}


def _del_repo_from_file(repo, filepath):
    """
    Remove a repo from filepath
    """
    with salt.utils.files.fopen(filepath) as fhandle:
        output = []
        regex = re.compile(REPO_REGEXP)
        for line in fhandle:
            line = salt.utils.stringutils.to_unicode(line)
            if regex.search(line):
                if line.startswith("#"):
                    line = line[1:]
                cols = salt.utils.args.shlex_split(line.strip())
                if repo != cols[1]:
                    output.append(salt.utils.stringutils.to_str(line))
    with salt.utils.files.fopen(filepath, "w") as fhandle:
        fhandle.writelines(output)


def _set_trusted_option_if_needed(repostr, trusted):
    """
    Set trusted option to repo if needed
    """
    if trusted is True:
        repostr += " [trusted=yes]"
    elif trusted is False:
        repostr += " [trusted=no]"
    return repostr


def _add_new_repo(repo, properties):
    """
    Add a new repo entry
    """
    repostr = "# " if not properties.get("enabled") else ""
    repostr += "src/gz " if properties.get("compressed") else "src "
    if " " in repo:
        repostr += '"' + repo + '" '
    else:
        repostr += repo + " "
    repostr += properties.get("uri")
    repostr = _set_trusted_option_if_needed(repostr, properties.get("trusted"))
    repostr += "\n"
    conffile = os.path.join(OPKG_CONFDIR, repo + ".conf")

    with salt.utils.files.fopen(conffile, "a") as fhandle:
        fhandle.write(salt.utils.stringutils.to_str(repostr))


def _mod_repo_in_file(repo, repostr, filepath):
    """
    Replace a repo entry in filepath with repostr
    """
    with salt.utils.files.fopen(filepath) as fhandle:
        output = []
        for line in fhandle:
            cols = salt.utils.args.shlex_split(
                salt.utils.stringutils.to_unicode(line).strip()
            )
            if repo not in cols:
                output.append(line)
            else:
                output.append(salt.utils.stringutils.to_str(repostr + "\n"))
    with salt.utils.files.fopen(filepath, "w") as fhandle:
        fhandle.writelines(output)


def del_repo(repo, **kwargs):  # pylint: disable=unused-argument
    """
    Delete a repo from ``/etc/opkg/*.conf``

    If the file does not contain any other repo configuration, the file itself
    will be deleted.

    CLI Examples:

    .. code-block:: bash

        salt '*' pkg.del_repo repo
    """
    refresh = salt.utils.data.is_true(kwargs.get("refresh", True))
    repos = list_repos()
    if repos:
        deleted_from = dict()
        for repository in repos:
            source = repos[repository][0]
            if source["name"] == repo:
                deleted_from[source["file"]] = 0
                _del_repo_from_file(repo, source["file"])

        if deleted_from:
            ret = ""
            for repository in repos:
                source = repos[repository][0]
                if source["file"] in deleted_from:
                    deleted_from[source["file"]] += 1
            for repo_file, count in deleted_from.items():
                msg = "Repo '{}' has been removed from {}.\n"
                if count == 1 and os.path.isfile(repo_file):
                    msg = "File {1} containing repo '{0}' has been removed.\n"
                    try:
                        os.remove(repo_file)
                    except OSError:
                        pass
                ret += msg.format(repo, repo_file)
            if refresh:
                refresh_db()
            return ret

    return "Repo {} doesn't exist in the opkg repo lists".format(repo)


def mod_repo(repo, **kwargs):
    """
    Modify one or more values for a repo.  If the repo does not exist, it will
    be created, so long as uri is defined.

    The following options are available to modify a repo definition:

    repo
        alias by which opkg refers to the repo.
    uri
        the URI to the repo.
    compressed
        defines (True or False) if the index file is compressed
    enabled
        enable or disable (True or False) repository
        but do not remove if disabled.
    refresh
        enable or disable (True or False) auto-refresh of the repositories

    CLI Examples:

    .. code-block:: bash

        salt '*' pkg.mod_repo repo uri=http://new/uri
        salt '*' pkg.mod_repo repo enabled=False
    """
    repos = list_repos()
    found = False
    uri = ""
    if "uri" in kwargs:
        uri = kwargs["uri"]
    allow_renaming = kwargs.get("allow_renaming", False)

    for repository in repos:
        source = repos[repository][0]
        if source["name"] == repo or (uri and source["uri"] == uri and allow_renaming):
            found = True
            repostr = ""
            if "enabled" in kwargs and not kwargs["enabled"]:
                repostr += "# "
            if "compressed" in kwargs:
                repostr += "src/gz " if kwargs["compressed"] else "src"
            else:
                repostr += "src/gz" if source["compressed"] else "src"
            repo_alias = kwargs["alias"] if "alias" in kwargs else repo
            if " " in repo_alias:
                repostr += ' "{}"'.format(repo_alias)
            else:
                repostr += " {}".format(repo_alias)
            repostr += " {}".format(kwargs["uri"] if "uri" in kwargs else source["uri"])
            trusted = kwargs.get("trusted")
            repostr = (
                _set_trusted_option_if_needed(repostr, trusted)
                if trusted is not None
                else _set_trusted_option_if_needed(repostr, source.get("trusted"))
            )
            _mod_repo_in_file(source["name"], repostr, source["file"])
        elif uri and source["uri"] == uri:
            raise CommandExecutionError(
                "Repository '{}' already exists as '{}'.".format(uri, source["name"])
            )

    if not found:
        # Need to add a new repo
        if "uri" not in kwargs:
            raise CommandExecutionError(
                "Repository '{}' not found and no URI passed to create one.".format(
                    repo
                )
            )
        properties = {"uri": kwargs["uri"]}
        # If compressed is not defined, assume True
        properties["compressed"] = (
            kwargs["compressed"] if "compressed" in kwargs else True
        )
        # If enabled is not defined, assume True
        properties["enabled"] = kwargs["enabled"] if "enabled" in kwargs else True
        properties["trusted"] = kwargs.get("trusted")
        _add_new_repo(repo, properties)

    if "refresh" in kwargs:
        refresh_db()


def file_list(*packages, **kwargs):  # pylint: disable=unused-argument
    """
    List the files that belong to a package. Not specifying any packages will
    return a list of _every_ file on the system's package database (not
    generally recommended).

    CLI Examples:

    .. code-block:: bash

        salt '*' pkg.file_list httpd
        salt '*' pkg.file_list httpd postfix
        salt '*' pkg.file_list
    """
    output = file_dict(*packages)
    files = []
    for package in list(output["packages"].values()):
        files.extend(package)
    return {"errors": output["errors"], "files": files}


def file_dict(*packages, **kwargs):  # pylint: disable=unused-argument
    """
    List the files that belong to a package, grouped by package. Not
    specifying any packages will return a list of _every_ file on the system's
    package database (not generally recommended).

    CLI Examples:

    .. code-block:: bash

        salt '*' pkg.file_list httpd
        salt '*' pkg.file_list httpd postfix
        salt '*' pkg.file_list
    """
    errors = []
    ret = {}
    cmd_files = ["opkg", "files"]

    if not packages:
        packages = list(list_pkgs().keys())

    for package in packages:
        files = []
        cmd = cmd_files[:]
        cmd.append(package)
        out = _call_opkg(cmd)
        for line in out["stdout"].splitlines():
            if line.startswith("/"):
                files.append(line)
            elif line.startswith(" * "):
                errors.append(line[3:])
                break
            else:
                continue
        if files:
            ret[package] = files

    return {"errors": errors, "packages": ret}


def owner(*paths, **kwargs):  # pylint: disable=unused-argument
    """
    Return the name of the package that owns the file. Multiple file paths can
    be passed. Like :mod:`pkg.version <salt.modules.opkg.version`, if a single
    path is passed, a string will be returned, and if multiple paths are passed,
    a dictionary of file/package name pairs will be returned.

    If the file is not owned by a package, or is not present on the minion,
    then an empty string will be returned for that path.

    CLI Example:

    .. code-block:: bash

        salt '*' pkg.owner /usr/bin/apachectl
        salt '*' pkg.owner /usr/bin/apachectl /usr/bin/basename
    """
    if not paths:
        return ""
    ret = {}
    cmd_search = ["opkg", "search"]
    for path in paths:
        cmd = cmd_search[:]
        cmd.append(path)
        output = __salt__["cmd.run_stdout"](
            cmd, output_loglevel="trace", python_shell=False
        )
        if output:
            ret[path] = output.split(" - ")[0].strip()
        else:
            ret[path] = ""
    if len(ret) == 1:
        return next(iter(ret.values()))
    return ret


def version_clean(version):
    """
    Clean the version string removing extra data.
    There's nothing do to here for nipkg.py, therefore it will always
    return the given version.
    """
    return version


def check_extra_requirements(pkgname, pkgver):
    """
    Check if the installed package already has the given requirements.
    There's nothing do to here for nipkg.py, therefore it will always
    return True.
    """
    return True
