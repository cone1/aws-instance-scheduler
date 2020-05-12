# AWS Instance Scheduler

## Description

Scheduler for Cross-Account and Cross-Region start/stop scheduling for EC2 and RDS instances

Merged from AWS Original & DanielRedOaks ASG Patch
- Instance Scheduler 1.3.1
- Python 3.7
- Aurora RDS support
- KMS support
- ASG Patch


## Setup

```
# Requirements

$ pip3.7 install certifi chardet idna pytz requests urllib3 -t code/


# Build

$ cd deployment
$ ./build-s3-dist.sh ischeduler-release-bucket ischeduler 1.3.1-asg


# Deploy

- Copy Lambda instance-scheduler.zip to S3
- Deploy CFN to service account & remote accounts
- Create schedules & Tag instances

https://docs.aws.amazon.com/solutions/latest/instance-scheduler/
```

***

Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.

Licensed under the Apache License Version 2.0 (the "License"). You may not use this file except in compliance with the License. A copy of the License is located at

    http://www.apache.org/licenses/

or in the "license" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and limitations under the License.
