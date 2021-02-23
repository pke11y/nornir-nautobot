"""default driver for the network_importer."""
# pylint: disable=raise-missing-from,too-many-arguments

import os
import re
import socket
import jinja2

from netmiko.ssh_exception import NetmikoAuthenticationException, NetmikoTimeoutException
from nornir.core.exceptions import NornirSubTaskError
from nornir.core.task import Result, Task
from nornir_jinja2.plugins.tasks import template_file
from nornir_napalm.plugins.tasks import napalm_get
from nornir_netmiko.tasks import netmiko_send_command


from nornir_nautobot.exceptions import NornirNautobotException

from .utils.compliance import compliance
from .utils.functions import make_folder, hostname_resolves, test_tcp_port, is_ip

RUN_COMMAND_MAPPING = {
    "default": "show run",
    "cisco_nxos": "show run",
    "cisco_ios": "show run",
    "cisco_xr": "show run",
    "juniper_junos": "show configuration | display set",
    "arista_eos": "show run",
}


class NautobotNornirDriver:
    """Default collection of Nornir Tasks based on Napalm."""

    @staticmethod
    def get_config(task: Task, logger, obj, backup_file: str, remove_lines: list, substitute_lines: list) -> Result:
        """Get the latest configuration from the device.

        Args:
            task (Task): Nornir Task

        Returns:
            Result: Nornir Result object with a dict as a result containing the running configuration
                { "config: <running configuration> }
        """
        logger.log_debug(f"Executing get_config for {task.host.name} on {task.host.platform}")

        # TODO: Find standard napalm exceptions and account for them
        try:
            result = task.run(task=napalm_get, getters=["config"], retrieve="running")
        except NornirSubTaskError as exc:
            logger.log_failure(obj, f"Failed with a unknown issue. `{exc.result.exception}`")
            raise NornirNautobotException()

        if result[0].failed:
            logger.log_failure(obj, f"Failed with a unknown issue. `{str(result.exception)}`")
            return result

        running_config = result[0].result.get("config", {}).get("running", None)
        if remove_lines:
            logger.log_debug("Removing lines from configuration based on `remove_lines` definition")
            running_config = _remove_lines(running_config, remove_lines)

        if substitute_lines:
            logger.log_debug("Substitute lines from configuration based on `substitute_lines` definition")
            running_config = _substitute_lines(running_config, substitute_lines)

        make_folder(os.path.dirname(backup_file))

        with open(backup_file, "w") as filehandler:
            filehandler.write(running_config)
        return Result(host=task.host, result={"config": running_config})

    @staticmethod
    def check_connectivity(task: Task, logger, obj) -> Result:
        """Get the latest configuration from the device."""
        if is_ip(task.host.hostname):
            ip_addr = task.host.hostname
        else:
            if not hostname_resolves(task.host.hostname):
                logger.log_failure(obj, "not an IP or resolvable.")
                raise NornirNautobotException("not an IP or resolvable.")
            ip_addr = socket.gethostbyname(task.host.hostname)

        # TODO: Allow port to be configurable
        port = 22
        if not test_tcp_port(ip_addr, port):
            logger.log_failure(obj, f"Attempting to connect to IP: {ip_addr} and port: {port} failed.")
            raise NornirNautobotException(f"Attempting to connect to IP: {ip_addr} and port: {port} failed.")
        if not task.host.username:
            logger.log_failure(obj, "There was no username defined, preemptively failed.")
            raise NornirNautobotException("There was no username defined, preemptively failed.")
        if not task.host.password:
            logger.log_failure(obj, "There was no password defined, preemptively failed.")
            raise NornirNautobotException("There was no password defined, preemptively failed.")

        return Result(host=task.host)

    @staticmethod
    def compliance_config(
        task: Task, logger, obj, features: str, backup_file: str, intended_file: str, platform: str
    ) -> Result:
        """Compare two configurations against each other."""
        if not os.path.exists(backup_file):
            logger.log_failure(obj, f"Backup file Not Found at location: `{backup_file}`")
            raise NornirNautobotException()

        if not os.path.exists(intended_file):
            logger.log_failure(obj, f"Intended config file NOT Found at location: `{intended_file}`")
            raise NornirNautobotException()

        try:
            feature_data = compliance(features, backup_file, intended_file, platform)
        except Exception as error:  # pylint: disable=broad-except
            logger.log_failure(obj, f"UNKNOWN Failure of: {str(error)}")
            raise NornirNautobotException()
        return Result(host=task.host, result={"feature_data": feature_data})

    @staticmethod
    def generate_config(
        task: Task, logger, obj, jinja_template: str, jinja_root_path: str, output_file_location: str
    ) -> Result:
        """Get the latest configuration from the device."""
        try:
            filled_template = task.run(
                **task.host.data,
                task=template_file,
                name="JINJA TEMPLATE CREATION",
                template=jinja_template,
                path=jinja_root_path,
            )[0].result
        except NornirSubTaskError as exc:
            if isinstance(exc.result.exception, jinja2.exceptions.UndefinedError):  # pylint: disable=no-else-raise
                logger.log_failure(
                    obj,
                    f"There was a jinja2.exceptions.UndefinedError error: ``{str(exc.result.exception)}``",
                )
                raise NornirNautobotException()
            elif isinstance(exc.result.exception, jinja2.TemplateSyntaxError):
                logger.log_failure(
                    obj,
                    f"There was a jinja2.TemplateSyntaxError error: ``{str(exc.result.exception)}``",
                )
                raise NornirNautobotException()
            elif isinstance(exc.result.exception, jinja2.TemplateNotFound):
                logger.log_failure(
                    obj,
                    f"There was an issue finding the template and a jinja2.TemplateNotFound error was raised: ``{str(exc.result.exception)}``",
                )
                raise NornirNautobotException()
            elif isinstance(exc.result.exception, jinja2.TemplateError):
                logger.log_failure(obj, f"There was an issue general Jinja error: ``{str(exc.result.exception)}``")
                raise NornirNautobotException()
            raise

        make_folder(os.path.dirname(output_file_location))
        with open(output_file_location, "w") as filehandler:
            filehandler.write(filled_template)
        return Result(host=task.host, result={"config": filled_template})


class NetmikoNautobotNornirDriver(NautobotNornirDriver):
    """Default collection of Nornir Tasks based on Netmiko."""

    @staticmethod
    def get_config(task: Task, logger, obj, backup_file: str, remove_lines: list, substitute_lines: list) -> Result:
        """Get the latest configuration from the device using Netmiko.

        Args:
            task (Task): Nornir Task

        Returns:
            Result: Nornir Result object with a dict as a result containing the running configuration
                { "config: <running configuration> }
        """
        logger.log_debug(f"Executing get_config for {task.host.name} on {task.host.platform}")
        command = RUN_COMMAND_MAPPING.get(task.host.platform, RUN_COMMAND_MAPPING["default"])

        try:
            result = task.run(task=netmiko_send_command, command_string=command)
        except NornirSubTaskError as exc:
            if isinstance(exc.result.exception, NetmikoAuthenticationException):
                logger.log_failure(obj, f"Failed with an authentication issue: `{exc.result.exception}`")
                raise NornirNautobotException()

            if isinstance(exc.result.exception, NetmikoTimeoutException):
                logger.log_failure(obj, f"Failed with a timeout issue. `{exc.result.exception}`")
                raise NornirNautobotException()

            logger.log_failure(obj, f"Failed with an unknown issue. `{exc.result.exception}`")
            raise NornirNautobotException()

        if result[0].failed:
            return result

        running_config = result[0].result

        # Primarily seen in Cisco devices.
        if "ERROR: % Invalid input detected at" in running_config:
            logger.log_failure(obj, "Discovered `ERROR: % Invalid input detected at` in the output")
            raise NornirNautobotException()

        if remove_lines:
            logger.log_debug("Removing lines from configuration based on `remove_lines` definition")
            running_config = _remove_lines(running_config, remove_lines)
        if substitute_lines:
            logger.log_debug("Substitute lines from configuration based on `substitute_lines` definition")
            running_config = _substitute_lines(running_config, substitute_lines)

        make_folder(os.path.dirname(backup_file))

        with open(backup_file, "w") as filehandler:
            filehandler.write(running_config)
        return Result(host=task.host, result={"config": running_config})


def _remove_lines(config, remove_lines):
    """Method to remove any lines that are required to be removed.

    Args:
        config (str): A string that represent the configuration of a device.
        remove_lines (list): A list of regex strings, that get converted to raw regex.

    Returns:
        config: The parse configuration, which is absent of any matches from remove_lines.
    """
    for removal in remove_lines:
        config = re.sub(fr"{removal}", "", config, flags=re.MULTILINE)
    return config


def _substitute_lines(config, substitute_lines):
    r"""Method to substitute any lines, the primary use case is removing secrets.

    Args:
        config (str): A string that represent the configuration of a device.
        substitute_lines (list): List of dictionaries representing the replacement. Each list item is a dictionary in the
        {
            "regex_replacement": "<redacted_config>",
            "regex_search": "username\\s+\\S+\\spassword\\s+5\\s+(\\S+)\\s+role\\s+\\S+"
        }

    Returns:
        config: The parse configuration, which has the lines swapped based on regex replacements.
    """
    if substitute_lines:
        new_config = ""

        # Loop through the config lines
        for line in config.split("\n"):
            # Loop through the replacement list on each line in the config
            for item in substitute_lines:
                if re.match(pattern=fr"{item['regex_search']}", string=line):
                    line = line.replace(
                        re.match(pattern=fr"{item['regex_search']}", string=line).groups()[0], item["regex_replacement"]
                    )

                new_config += line.rstrip() + "\n"

        config = new_config
    return config
