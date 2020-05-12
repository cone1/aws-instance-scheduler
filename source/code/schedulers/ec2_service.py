######################################################################################################################
#  Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.                                           #
#                                                                                                                    #
#  Licensed under the Apache License Version 2.0 (the "License"). You may not use this file except in compliance     #
#  with the License. A copy of the License is located at                                                             #
#                                                                                                                    #
#      http://www.apache.org/licenses/                                                                               #
#                                                                                                                    #
#  or in the "license" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES #
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions    #
#  and limitations under the License.                                                                                #
######################################################################################################################
import os
import time
from datetime import timedelta

import dateutil
import jmespath
from botocore.exceptions import ClientError
import itertools
import re

import configuration
import schedulers
import time
from boto_retry import get_client_with_retries
from configuration import SchedulerConfigBuilder
from configuration.instance_schedule import InstanceSchedule
from configuration.running_period import RunningPeriod
from time import sleep

# instances are started in batches, larger bathes are more efficient but smaller batches allow more instances
# to start if we run into resource limits

ERR_RESIZING_INSTANCE_ = "Error resizing instance {}, ({})"

START_BATCH_SIZE = 5
STOP_BATCH_SIZE = 50
STANDBY_BATCH_SIZE = 20

ERR_STARTING_INSTANCES = "Error starting instances {}, ({})"
ERR_STOPPING_INSTANCES = "Error stopping instances {}, ({})"
ERR_MAINT_WINDOW_NOT_FOUND_OR_DISABLED = "SSM maintenance window {} used in schedule {} not found or disabled"
ERR_EXITING_STANDBY_INSTANCES = "Error exiting standby for instances {}, ({}): {}"

INF_FETCHED_INSTANCES = "Number of fetched ec2 instances is {}, number of instances in a schedulable state is {}"
INF_FETCHING_INSTANCES = "Fetching ec2 instances for account {} in region {}"
INF_SETTING_SIZE = "Setting size for ec2 instance {} to {}"
INF_ADD_KEYS = "Adding {} key(s) {} to instance(s) {}"
INFO_REMOVING_KEYS = "Removing {} key(s) {} from instance(s) {}"
INF_MAINT_WINDOW = "Created schedule {} from SSM maintence window, start is {}, end is {}"
INF_MAINT_WINDOW_DISABLED = "SSM maintenance window {} ({}) is disabled"
INF_FETCHING_ASG_INSTANCES = "Fetching schedulable ec2 instances belonging to an autoscaling group for account {} in region {}"
INF_FETCHED_ASG_INSTANCES = "Number of fetched schedulable ec2 instances that belong to an autoscaling group: {}"
INF_STANDBY_MODE = "{} standby mode for the instances: {} in autoscaling group: {}"

WARN_STARTED_INSTANCES_TAGGING = "Error deleting or creating tags for started instances {} ({})"
WARN_STOPPED_INSTANCES_TAGGING = "Error deleting or creating tags for stopped instances {} ({})"
WARNING_INSTANCE_NOT_STARTING = "Ec2 instance {} is not started"
WARNING_INSTANCE_NOT_STOPPING = "Ec2 instance {} is not stopped"
WARN_NOT_HIBERNATED = "Instance {} could not be hibernated, retry to stop without hibernation, {}"
WARN_NO_HIBERNATE_RESIZED = "Instance {} is not hibernated because it is stopped for resizing the instance"
WARN_STANDBY_GROUP = "Action {} standby failed for the instance group {} in autoscaling group {}. Will retry individually."
WARN_STANDBY_FAILED_ENTER = "Instances {} failed to enter standby mode, they will not be stopped."
WARN_STANDBY_FAILED_EXIT = "Instances {} were started but failed to exit standby mode."

DEBUG_SKIPPED_INSTANCE = "Skipping ec2 instance {} because it it not in a schedulable state ({})"
DEBUG_SELECTED_INSTANCE = "Selected ec2 instance {} in state ({})"

class Ec2Service:
    """
    Implements service start/stop/resize functions for EC2 service
    """
    EC2_STATE_PENDING = 0
    EC2_STATE_RUNNING = 16
    EC2_STATE_SHUTTING_DOWN = 32
    EC2_STATE_TERMINATED = 48
    EC2_STATE_STOPPING = 64
    EC2_STATE_STOPPED = 80

    EC2_SCHEDULABLE_STATES = {EC2_STATE_RUNNING, EC2_STATE_STOPPED}
    EC2_STOPPING_STATES = {EC2_STATE_SHUTTING_DOWN, EC2_STATE_STOPPING, EC2_STATE_STOPPED}
    EC2_STARTING_STATES = {EC2_STATE_PENDING, EC2_STATE_RUNNING}

    def __init__(self):
        self.service_name = "ec2"
        self.allow_resize = True
        self.schedules_with_hibernation = []
        self._ssm_maintenance_windows = None
        self._session = None
        self._logger = None
        self.supports_standby = True


    def _init_scheduler(self, args):
        self._session = args.get(schedulers.PARAM_SESSION)
        self._context = args.get(schedulers.PARAM_CONTEXT)
        self._region = args.get(schedulers.PARAM_REGION)
        self._logger = args.get(schedulers.PARAM_LOGGER)
        self._account = args.get(schedulers.PARAM_ACCOUNT)
        self._tagname = args.get(schedulers.PARAM_TAG_NAME)

    @classmethod
    def instance_batches(cls, instances, size):
        instance_buffer = []
        retries = []
        while True:
            for instance in instances:
                instance_buffer.append(instance)
                if len(instance_buffer) == size:
                    retry = yield instance_buffer
                    if retry:
                        retries.extend(retry)
                        yield
                    instance_buffer = []
            if len(instance_buffer) > 0:
                retry = yield instance_buffer
                if retry:
                    retries.extend(retry)
                    yield
            for retry_instance in retries:
                yield [retry_instance]
            break

    @property
    def ssm_maintenance_windows(self):
        if self._ssm_maintenance_windows is None:
            self._ssm_maintenance_windows = {}

            ssm = self._session.client("ssm")

            try:
                args = {}
                while True:
                    resp = ssm.describe_maintenance_windows(**args)
                    for window in resp.get("WindowIdentities", []):
                        if not window["Enabled"]:
                            self._logger.info(INF_MAINT_WINDOW_DISABLED, window["Name"], window['WindowId'])
                            continue

                        start = dateutil.parser.parse(window["NextExecutionTime"])
                        scheduler_timezone = window.get("ScheduleTimezone", "UTC")
                        scheduler_interval = max(10, int(os.getenv(configuration.ENV_SCHEDULE_FREQUENCY)))
                        maintenace_schedule = self._schedule_from_maint_window(name=window["Name"],
                                                                               start=start,
                                                                               interval=scheduler_interval,
                                                                               hours=window["Duration"],
                                                                               timezone=scheduler_timezone)
                        self._ssm_maintenance_windows[window["Name"]] = maintenace_schedule

                    next_token = resp.get("NextToken")
                    if next_token is None:
                        break

                    args["NextToken"] = next_token

            except Exception as ex:
                self._logger.error("Error loading ssm maintenace windows, ({})".format(ex))

        return self._ssm_maintenance_windows

    # get instances and handle paging
    def get_schedulable_instances(self, kwargs):
        self._session = kwargs[schedulers.PARAM_SESSION]
        context = kwargs[schedulers.PARAM_CONTEXT]
        region = kwargs[schedulers.PARAM_REGION]
        account = kwargs[schedulers.PARAM_ACCOUNT]
        self._logger = kwargs[schedulers.PARAM_LOGGER]
        tagname = kwargs[schedulers.PARAM_CONFIG].tag_name
        config = kwargs[schedulers.PARAM_CONFIG]

        self.schedules_with_hibernation = [s.name for s in config.schedules.values() if s.hibernate]

        client = get_client_with_retries("ec2", ["describe_instances"], context=context, session=self._session, region=region)
        asg_client = get_client_with_retries("autoscaling", ["describe_auto_scaling_instances"], context=context, session=self._session, region=region)

        def is_in_schedulable_state(ec2_inst):
            state = ec2_inst["state"] & 0xFF
            return state in Ec2Service.EC2_SCHEDULABLE_STATES

        jmes = "Reservations[*].Instances[*].{InstanceId:InstanceId, EbsOptimized:EbsOptimized, Tags:Tags, " \
               "InstanceType:InstanceType,State:State}[]" + \
               "|[?Tags]|[?contains(Tags[*].Key, '{}')]".format(tagname)

        args = {}
        asg_args = {}
        number_of_instances = 0
        instances = []
        asg_matched_instances = 0
        done = False

        self._logger.info(INF_FETCHING_INSTANCES, account, region)

        while not done:

            ec2_resp = client.describe_instances_with_retries(**args)
            for reservation_inst in jmespath.search(jmes, ec2_resp):
                inst = self._select_instance_data(instance=reservation_inst, tagname=tagname, config=config)
                number_of_instances += 1
                if is_in_schedulable_state(inst):
                    instances.append(inst)
                    self._logger.debug(DEBUG_SELECTED_INSTANCE, inst[schedulers.INST_ID], inst[schedulers.INST_STATE_NAME])
                else:
                    self._logger.debug(DEBUG_SKIPPED_INSTANCE, inst[schedulers.INST_ID], inst[schedulers.INST_STATE_NAME])
            if "NextToken" in ec2_resp:
                args["NextToken"] = ec2_resp["NextToken"]
            else:
                done = True
        self._logger.info(INF_FETCHED_INSTANCES, number_of_instances, len(instances))

        self._logger.info(INF_FETCHING_ASG_INSTANCES, account, region)
        done = False
        while not done:
            asg_resp = asg_client.describe_auto_scaling_instances_with_retries(**asg_args)
            for asg_inst in asg_resp["AutoScalingInstances"]:
                # determine if its one we can schedule, then add the asg_name & lifecycle to it
                instance = {}
                try:
                    instance = (ec2_inst for ec2_inst in instances if ec2_inst["id"] == asg_inst["InstanceId"]).__next__()
                except StopIteration:
                    pass
                if instance:
                    asg_matched_instances += 1
                    instance['allow_resize'] = False
                    instance['asg_name'] = asg_inst["AutoScalingGroupName"]
                    instance['asg_lifecycle_state'] = asg_inst["LifecycleState"]
                else:
                    instance['asg_name'] = None
                    instance['asg_lifecycle_state'] = None
            if "NextToken" in asg_resp:
                asg_args["NextToken"] = asg_resp["NextToken"]
            else:
                done = True
        self._logger.info(INF_FETCHED_ASG_INSTANCES, asg_matched_instances)
        return instances

    def _schedule_from_maint_window(self, name, start, hours, interval, timezone):
        start_dt = start.replace(second=0, microsecond=0)
        start_before_begin = min(interval, 10)
        begin_dt = start_dt - timedelta(minutes=start_before_begin)
        end_dt = start_dt + timedelta(hours=hours)
        if begin_dt.day == end_dt.day:
            periods = [
                {
                    "period":
                        RunningPeriod(
                            name="{}-period".format(name),
                            begintime=begin_dt.time(),
                            endtime=end_dt.time(),
                            monthdays={begin_dt.day},
                            months={begin_dt.month}
                        ),
                    "instancetype": None
                }
            ]
        elif end_dt - begin_dt <= timedelta(hours=24):
            periods = [
                {
                    "period":
                        RunningPeriod(
                            name="{}-period-1".format(name),
                            begintime=begin_dt.time(),
                            endtime=SchedulerConfigBuilder.get_time_from_string("23:59"),
                            monthdays={begin_dt.day},
                            months={begin_dt.month}
                        ),
                    "instancetype": None
                },
                {
                    "period":
                        RunningPeriod(
                            name="{}-period-2".format(name),
                            begintime=SchedulerConfigBuilder.get_time_from_string("00:00"),
                            endtime=end_dt.time(),
                            monthdays={end_dt.day},
                            months={end_dt.month}
                        ),
                    "instancetype": None
                }
            ]
        else:
            periods = [
                {
                    "period":
                        RunningPeriod(
                            name="{}-period-1".format(name),
                            begintime=begin_dt.time(),
                            endtime=SchedulerConfigBuilder.get_time_from_string("23:59"),
                            monthdays={begin_dt.day},
                            months={begin_dt.month}
                        ),
                    "instancetype": None
                },
                {
                    "period":
                        RunningPeriod(
                            name="{}-period-2".format(name),
                            monthdays={(end_dt - timedelta(days=1)).day},
                            months={(end_dt - timedelta(days=1)).month}
                        ),
                    "instancetype": None
                },
                {
                    "period":
                        RunningPeriod(
                            name="{}-period-3".format(name),
                            begintime=SchedulerConfigBuilder.get_time_from_string("00:00"),
                            endtime=end_dt.time(),
                            monthdays={end_dt.day},
                            months={end_dt.month}
                        ),
                    "instancetype": None
                }
            ]

        schedule = InstanceSchedule(name=name,
                                    timezone=timezone,
                                    description="{} maintenance window".format(name),
                                    enforced=True,
                                    periods=periods)

        self._logger.info(INF_MAINT_WINDOW, name, begin_dt.isoformat(), end_dt.isoformat())

        return schedule

    # selects and builds a named tuple for the instance data
    def _select_instance_data(self, instance, tagname, config):

        def get_tags(inst):
            return {tag["Key"]: tag["Value"] for tag in inst["Tags"]} if "Tags" in inst else {}

        tags = get_tags(instance)
        name = tags.get("Name", "")
        instance_id = instance["InstanceId"]
        state = instance["State"]["Code"] & 0XFF
        is_running = self.EC2_STATE_RUNNING == state
        is_terminated = state == Ec2Service.EC2_STATE_TERMINATED
        schedule_name = tags.get(tagname)

        maintenance_window_schedule = None
        schedule = config.schedules.get(schedule_name, None)
        if schedule is not None:
            if schedule.use_maintenance_window and schedule.ssm_maintenance_window not in [None, ""]:
                maintenance_window_schedule = self.ssm_maintenance_windows.get(schedule.ssm_maintenance_window, None)
                if maintenance_window_schedule is None:
                    self._logger.error(ERR_MAINT_WINDOW_NOT_FOUND_OR_DISABLED, schedule.ssm_maintenance_window, schedule.name)
                    self._ssm_maintenance_windows[schedule.ssm_maintenance_window] = "NOT-FOUND"
                if maintenance_window_schedule == "NOT-FOUND":
                    maintenance_window_schedule = None

        instance_data = {
            schedulers.INST_ID: instance_id,
            schedulers.INST_SCHEDULE: schedule_name,
            schedulers.INST_HIBERNATE: schedule_name in self.schedules_with_hibernation,
            schedulers.INST_NAME: name,
            schedulers.INST_STATE: state,
            schedulers.INST_STATE_NAME: instance["State"]["Name"],
            schedulers.INST_ALLOW_RESIZE: self.allow_resize,
            schedulers.INST_RESIZED: False,
            schedulers.INST_IS_RUNNING: is_running,
            schedulers.INST_IS_TERMINATED: is_terminated,
            schedulers.INST_CURRENT_STATE: InstanceSchedule.STATE_RUNNING if is_running else InstanceSchedule.STATE_STOPPED,
            schedulers.INST_INSTANCE_TYPE: instance["InstanceType"],
            schedulers.INST_TAGS: tags,
            schedulers.INST_MAINTENANCE_WINDOW: maintenance_window_schedule,
            schedulers.INST_ASG: None,
            schedulers.INST_ASG_LIFECYCLE_STATE: None
        }
        return instance_data

    # noinspection PyMethodMayBeStatic
    def resize_instance(self, kwargs):

        self._init_scheduler(kwargs)
        instance = kwargs[schedulers.PARAM_INSTANCE]
        instance_type = kwargs[schedulers.PARAM_DESIRED_TYPE]

        client = get_client_with_retries("ec2", ["modify_instance_attribute"],
                                         context=self._context, session=self._session, region=self._region)

        self._logger.info(INF_SETTING_SIZE, instance.id, instance_type)

        try:
            client.modify_instance_attribute_with_retries(InstanceId=instance.id, InstanceType={"Value": instance_type})
        except Exception as ex:
            self._logger.error(ERR_RESIZING_INSTANCE_, ",".join(instance.id), str(ex))

    def group_asg_instances(self, instances):
        # prepare instances that are a part of an asg, sort, and group for batch processing
        asg_instances = [instance for instance in instances if instance.asg_name]
        asg_instances.sort(key=lambda asg_instance: asg_instance.asg_name)
        grouped_instances = itertools.groupby(asg_instances, lambda asg_instance: asg_instance.asg_name)
        return grouped_instances

    def exit_standby_instances(self, **kwargs):
        self._init_scheduler(kwargs)
        standby_instances = []
        for asg_name, instances in self.group_asg_instances(kwargs[schedulers.PARAM_EXITED_STANDBY_INSTANCES]):
            batches = self.instance_batches(instances, STANDBY_BATCH_SIZE)
            for asg_instance_batch in batches:
                asg_instance_ids = [i.id for i in list(asg_instance_batch)]

                self._logger.info(INF_STANDBY_MODE, 'Exiting', asg_instance_ids, asg_name)
                asg_client = get_client_with_retries("autoscaling", ["exit_standby"], context=self._context,
                                                     session=self._session, region=self._region)
                try:
                    resp = asg_client.exit_standby_with_retries(InstanceIds=asg_instance_ids,
                                                                AutoScalingGroupName=asg_name)
                    for activity in resp['Activities']:
                        standby_instances.append((re.search(r"^Moving EC2 instance out of Standby: (i-.*)$",
                                                    activity['Description']).group(1), InstanceSchedule.STATE_RUNNING))
                except Exception as ex:
                    if len(asg_instance_ids) > 1:
                        # group failed, send the generator the retries
                        self._logger.warning(WARN_STANDBY_GROUP, 'exit', asg_instance_ids, asg_name)
                        batches.send(asg_instance_batch)
                    else:
                        not_instandby_regex = re.search(r"The instance (i-.*) is not in Standby.",
                                                  str(ex))
                        if not_instandby_regex:
                            standby_instances.append((not_instandby_regex.group(1), InstanceSchedule.STATE_RUNNING))
                        else:
                            self._logger.debug(ERR_EXITING_STANDBY_INSTANCES, asg_instance_ids, asg_name, repr(ex))
        return standby_instances

    def enter_standby_instances(self, instances):
        standby_instances = []
        failed_instances = []

        for asg_name, instances in self.group_asg_instances(instances):
            batches = self.instance_batches(instances, STANDBY_BATCH_SIZE)
            for asg_instance_batch in batches:
                asg_instance_ids = [i.id for i in list(asg_instance_batch)]

                self._logger.info(INF_STANDBY_MODE, 'Entering', asg_instance_ids, asg_name)
                asg_client = get_client_with_retries("autoscaling", ["enter_standby"], context=self._context,
                                                     session=self._session, region=self._region)
                try:
                    resp = asg_client.enter_standby_with_retries(InstanceIds=asg_instance_ids,
                                                                 AutoScalingGroupName=asg_name,
                                                                 ShouldDecrementDesiredCapacity=True)
                    for activity in resp['Activities']:
                        standby_instances.extend([
                            re.search(r"^Moving EC2 instance to Standby: (i-.*)$", activity['Description']).group(
                                1)])
                except Exception as ex:
                    if len(asg_instance_ids) > 1:
                        # group failed, send the generator the retries
                        self._logger.warning(WARN_STANDBY_GROUP, 'enter', asg_instance_ids, asg_name)
                        batches.send(asg_instance_batch)
                    else:  # working on individual instance
                        not_inservice = re.search(r"The instance (i-.*) is not in InService.",
                                                  str(ex))
                        if not_inservice:
                            self._logger.warning('Instance {} is already not InService', not_inservice.group(1))
                            standby_instances.extend(not_inservice.group(1).split(","))
                        else:
                            failed_instances.extend(asg_instance_ids)
        return standby_instances, failed_instances

    # noinspection PyMethodMayBeStatic
    def get_instance_status(self, client, instance_ids):
        status_resp = client.describe_instances_with_retries(InstanceIds=instance_ids)
        jmes = "Reservations[*].Instances[*].{InstanceId:InstanceId, State:State}[]"
        return jmespath.search(jmes, status_resp)

    # noinspection PyMethodMayBeStatic
    def stop_instances(self, kwargs):

        def is_in_stopping_state(state):
            return (state & 0xFF) in Ec2Service.EC2_STOPPING_STATES

        self._init_scheduler(kwargs)

        stopped_instances = kwargs[schedulers.PARAM_STOPPED_INSTANCES]

        # handle asg instances, do not stop those that fail
        standby_instances, failed_exit_standby = self.enter_standby_instances(stopped_instances)
        if failed_exit_standby:
            self._logger.warning(WARN_STANDBY_FAILED_ENTER, failed_exit_standby)
            stopped_instances = [instance for instance in stopped_instances if instance.id not in failed_exit_standby]

        stop_tags = kwargs[schedulers.PARAM_CONFIG].stopped_tags
        if stop_tags is None:
            stop_tags = []
        stop_tags_key_names = [t["Key"] for t in stop_tags]

        start_tags_keys = [{"Key": t["Key"]} for t in kwargs[schedulers.PARAM_CONFIG].started_tags if
                           t["Key"] not in stop_tags_key_names]

        methods = ["stop_instances", "create_tags", "delete_tags", "describe_instances"]
        client = get_client_with_retries("ec2", methods=methods, context=self._context, session=self._session, region=self._region)

        for instance_batch in list(self.instance_batches(stopped_instances, STOP_BATCH_SIZE)):

            instance_ids = [i.id for i in instance_batch]

            # split in hibernated and non hibernated, instanced that are stopped for resizing cannot be hibernated
            hibernated = [i.id for i in instance_batch if i.hibernate and not i.resized]
            not_hibernated = [i.id for i in instance_batch if i.id not in hibernated]

            for inst in [i for i in instance_batch if i.hibernate and i.resized]:
                self._logger.warning(WARN_NO_HIBERNATE_RESIZED, inst.id)

            instances_stopping = []

            try:
                while len(hibernated) > 0:
                    try:
                        stop_resp = client.stop_instances_with_retries(InstanceIds=hibernated, Hibernate=True)
                        instances_stopping += [i["InstanceId"] for i in stop_resp.get("StoppingInstances", []) if
                                               is_in_stopping_state(i.get("CurrentState", {}).get("Code", ""))]
                        break
                    except ClientError as ex:
                        instance_id = None
                        if ex.response.get("Error", {}).get("Code") == "UnsupportedHibernationConfiguration":
                            instance_id = ex.response["Error"]["Message"].split(":")[-1].strip()
                        elif ex.response.get("Error", {}).get("Code") == "UnsupportedOperation":
                            instance_id = ex.response["Error"]["Message"].split(" ")[1].strip()
                        if instance_id in hibernated:
                            self._logger.warning(WARN_NOT_HIBERNATED, instance_id, ex)
                            hibernated.remove(instance_id)
                            not_hibernated.append(instance_id)
                        else:
                            self._logger.error(ERR_STOPPING_INSTANCES, ",".join(hibernated), str(ex))

                if len(not_hibernated) > 0:
                    try:
                        stop_resp = client.stop_instances_with_retries(InstanceIds=not_hibernated, Hibernate=False)
                        instances_stopping += [i["InstanceId"] for i in stop_resp.get("StoppingInstances", []) if
                                               is_in_stopping_state(i.get("CurrentState", {}).get("Code", ""))]
                    except Exception as ex:
                        self._logger.error(ERR_STOPPING_INSTANCES, ",".join(not_hibernated), str(ex))

                get_status_count = 0
                if len(instances_stopping) < len(instance_ids):
                    time.sleep(5)

                    instances_stopping = [i["InstanceId"] for i in self.get_instance_status(client, instance_ids) if
                                          is_in_stopping_state(i.get("State", {}).get("Code", ""))]

                    if len(instances_stopping) == len(instance_ids):
                        break

                    get_status_count += 1
                    if get_status_count > 3:
                        for i in instance_ids:
                            if i not in instances_stopping:
                                self._logger.warning(WARNING_INSTANCE_NOT_STOPPING, i)
                        break

                if len(instances_stopping) > 0:
                    try:
                        if start_tags_keys is not None and len(start_tags_keys):
                            self._logger.info(INFO_REMOVING_KEYS, "start",
                                              ",".join(["\"{}\"".format(k["Key"]) for k in start_tags_keys]),
                                              ",".join(instances_stopping))
                            client.delete_tags_with_retries(Resources=instances_stopping, Tags=start_tags_keys)
                        if len(stop_tags) > 0:
                            self._logger.info(INF_ADD_KEYS, "stop", str(stop_tags), ",".join(instances_stopping))
                            client.create_tags_with_retries(Resources=instances_stopping, Tags=stop_tags)
                    except Exception as ex:
                        self._logger.warning(WARN_STOPPED_INSTANCES_TAGGING, ','.join(instances_stopping), str(ex))

                for i in instances_stopping:
                    yield i, InstanceSchedule.STATE_STOPPED

            except Exception as ex:
                self._logger.error(ERR_STOPPING_INSTANCES, ",".join(instance_ids), str(ex))

    # noinspection PyMethodMayBeStatic
    def start_instances(self, kwargs):

        def is_in_starting_state(state):
            return (state & 0xFF) in Ec2Service.EC2_STARTING_STATES

        self._init_scheduler(kwargs)

        instances_to_start = kwargs[schedulers.PARAM_STARTED_INSTANCES]
        started_instances = kwargs[schedulers.PARAM_STARTED_INSTANCES]

        start_tags = kwargs[schedulers.PARAM_CONFIG].started_tags
        if start_tags is None:
            start_tags = []
        start_tags_key_names = [t["Key"] for t in start_tags]
        stop_tags_keys = [{"Key": t["Key"]} for t in kwargs[schedulers.PARAM_CONFIG].stopped_tags if
                          t["Key"] not in start_tags_key_names]
        client = get_client_with_retries("ec2", ["start_instances", "describe_instances", "create_tags", "delete_tags"],
                                         context=self._context, session=self._session, region=self._region)

        for instance_batch in self.instance_batches(instances_to_start, START_BATCH_SIZE):

            instance_ids = [i.id for i in list(instance_batch)]
            try:
                start_resp = client.start_instances_with_retries(InstanceIds=instance_ids)
                instances_starting = [i["InstanceId"] for i in start_resp.get("StartingInstances", []) if
                                      is_in_starting_state(i.get("CurrentState", {}).get("Code", ""))]

                get_status_count = 0
                if len(instances_starting) < len(instance_ids):
                    time.sleep(5)

                    instances_starting = [i["InstanceId"] for i in self.get_instance_status(client, instance_ids) if
                                          is_in_starting_state(i.get("State", {}).get("Code", ""))]

                    if len(instances_starting) == len(instance_ids):
                        break

                    get_status_count += 1
                    if get_status_count > 3:
                        for i in instance_ids:
                            if i not in instances_starting:
                                self._logger.warning(WARNING_INSTANCE_NOT_STARTING, i)
                        break

                if len(instances_starting) > 0:
                    try:
                        if stop_tags_keys is not None and len(stop_tags_keys) > 0:
                            self._logger.info(INFO_REMOVING_KEYS, "stop",
                                              ",".join(["\"{}\"".format(k["Key"]) for k in stop_tags_keys]),
                                              ",".join(instances_starting))
                            client.delete_tags_with_retries(Resources=instances_starting, Tags=stop_tags_keys)
                        if len(start_tags) > 0:
                            self._logger.info(INF_ADD_KEYS, "start", str(start_tags), ",".join(instances_starting))
                            client.create_tags_with_retries(Resources=instances_starting, Tags=start_tags)
                    except Exception as ex:
                        self._logger.warning(WARN_STARTED_INSTANCES_TAGGING, ','.join(instances_starting), str(ex))

                for i in instances_starting:
                    yield i, InstanceSchedule.STATE_RUNNING

                for started_instance in start_resp["StartingInstances"]:
                    if is_in_starting_state(started_instance["CurrentState"]["Code"]):
                        instances_starting.append(started_instance["InstanceId"])
                        asg_instance = {}
                        try:  # match instance to object in instance_batch
                            asg_instance = (ec2_inst for ec2_inst in instance_batch if
                                        ec2_inst.id == started_instance["InstanceId"]).__next__()
                        except StopIteration:
                            pass
                        if asg_instance != {} and asg_instance.asg_lifecycle_state == "Standby":
                            yield started_instance["InstanceId"], InstanceSchedule.STATE_STANDBY
                        else:
                            yield started_instance["InstanceId"], InstanceSchedule.STATE_RUNNING
                    else:
                        self._logger.warning(WARNING_INSTANCE_NOT_STARTING, started_instance["InstanceId"])
            except Exception as ex:
                self._logger.error(ERR_STARTING_INSTANCES, ",".join(instance_ids), str(ex))
