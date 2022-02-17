# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import boto3
import random
import logging
import threading
from .util import make_zip_file
from .command_builder import BashBuilder
from .dds_config_builder import CycloneConfigBuilder
from .scp import SCP_Client
import os

class CloudInstance:
    def __init__(self):
        self.cyclone_builder = None
        self.scp = None
        self.public_ip = None
        self.ssh_key_path = None
        self.name = None
        self.unique_name = str(random.randint(10, 1000))

        self.ready_lock = threading.Lock()
        self.ready_state = False

    def create(self):
        raise NotImplementedError("Cloud SuperClass not implemented")

    def connect(self):
        self.scp = SCP_Client(self.public_ip, self.ssh_key_path)
        self.scp.connect()

    def get_ssh_key_path(self):
        return self.ssh_key_path

    def get_ip(self):
        return self.public_ip

    def get_name(self):
        return self.unique_name

    def get_ready_state(self):
        self.ready_lock.acquire()
        ready = self.ready_state
        self.ready_lock.release()
        return ready

    def set_ready_state(self, ready = True):
        self.ready_lock.acquire()
        self.ready_state = ready
        self.ready_lock.release()
        return ready

    def install_cloud_dependencies(self):
        self.scp.execute_cmd("sudo apt install -y wireguard unzip")
        self.scp.execute_cmd("sudo pip3 install wgconfig boto3 paramiko scp")

    def push_ros_workspace(self):
        # configure ROS env
        workspace_path = "/home/root/fog_ws" #os.getenv("COLCON_PREFIX_PATH")

        zip_dst = "/tmp/ros_workspace"
        make_zip_file(workspace_path, zip_dst)
        self.scp.execute_cmd("echo removing old workspace")
        self.scp.execute_cmd("rm -rf ros_workspace.zip ros2_ws fog_ws")
        self.scp.send_file(zip_dst + ".zip", "/home/ubuntu/")
        self.scp.execute_cmd("unzip -q /home/ubuntu/ros_workspace.zip")
        self.scp.execute_cmd("echo successfully extracted new workspace")

    def push_to_cloud_nodes(self):
        self.scp.send_file("/tmp/to_cloud_" + self.unique_name, "/tmp/to_cloud_nodes")

    def push_and_setup_vpn(self):
        self.scp.send_file("/tmp/fogros-cloud.conf"+ self.unique_name, "/tmp/fogros-aws.conf")
        self.scp.execute_cmd(
            "sudo cp /tmp/fogros-aws.conf /etc/wireguard/wg0.conf && sudo chmod 600 /etc/wireguard/wg0.conf && sudo wg-quick up wg0"
        )

    def configure_DDS(self):
        # configure DDS
        self.cyclone_builder = CycloneConfigBuilder(["10.0.0.1"])
        self.cyclone_builder.generate_config_file()
        self.scp.send_file("/tmp/cyclonedds.xml", "~/cyclonedds.xml")

    def launch_cloud_node(self):
        cmd_builder = BashBuilder()
        cmd_builder.append("source /home/ubuntu/ros2_rolling/install/setup.bash")
        cmd_builder.append("cd /home/ubuntu/fog_ws && colcon build --merge-install")
        cmd_builder.append(". /home/ubuntu/fog_ws/install/setup.bash")
        cmd_builder.append(self.cyclone_builder.env_cmd)
        cmd_builder.append("ros2 launch fogros2 cloud.launch.py")
        print(cmd_builder.get())
        self.scp.execute_cmd(cmd_builder.get())

class RemoteMachine(CloudInstance):
    def __init__(self, ip, ssh_key_path):
        super().__init__()
        self.ip = public_ip
        self.ssh_key_path = ssh_key_path
        self.unique_name = "REMOTE" + str(random.randint(10, 1000))
        self.set_ready_state() # assume it to be true
    
    def create(self):
        # since the machine is assumed to be established
        # no need to create
        pass


class AWS(CloudInstance):
    def __init__(
        self,
        region="us-west-1",
        store_key_path="/home/root/fog_ws/",
        ec2_instance_type="t2.micro",
    ):
        super().__init__()
        self.region = region
        self.ec2_instance_type = ec2_instance_type
        self.ec2_instance_disk_size = 30  # GB
        self.aws_ami_image = "ami-08b3b42af12192fe6"

        # key & security group names
        self.unique_name = "AWS" + str(random.randint(10, 1000))
        self.ec2_security_group = "FOGROS_SECURITY_GROUP" + self.unique_name
        self.ec2_key_name = "FogROSKEY" + self.unique_name
        self.ssh_key_path = store_key_path + self.ec2_key_name + ".pem"

        # aws objects
        self.ec2_instance = None
        self.ec2_resource_manager = boto3.resource("ec2", self.region)
        self.ec2_boto3_client = boto3.client("ec2", self.region)

        # after config

        self.ssh_key = None
        self.ec2_security_group_ids = None

        # others
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.WARNING)

        self.create()

    def create(self):
        print("creating EC2 instance")
        self.create_security_group()
        self.generate_key_pair()
        self.create_ec2_instance()
        self.connect()
        self.install_cloud_dependencies()
        self.push_ros_workspace()
        #self.push_to_cloud_nodes()
        self.set_ready_state()

    def get_ssh_key(self):
        return self.ssh_key

    def create_security_group(self):
        response = self.ec2_boto3_client.describe_vpcs()
        vpc_id = response.get("Vpcs", [{}])[0].get("VpcId", "")
        try:
            response = self.ec2_boto3_client.create_security_group(
                GroupName=self.ec2_security_group,
                Description="DESCRIPTION",
                VpcId=vpc_id,
            )
            security_group_id = response["GroupId"]
            self.logger.info(
                "Security Group Created %s in vpc %s." % (security_group_id, vpc_id)
            )

            data = self.ec2_boto3_client.authorize_security_group_ingress(
                GroupId=security_group_id,
                IpPermissions=[
                    {
                        "IpProtocol": "-1",
                        "FromPort": 0,
                        "ToPort": 65535,
                        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                    }
                ],
            )
            self.logger.info("Ingress Successfully Set %s" % data)
            ec2_security_group_ids = [security_group_id]
        except ClientError as e:
            self.logger.error(e)
        self.logger.warn("security group id is " + str(ec2_security_group_ids))
        self.ec2_security_group_ids = ec2_security_group_ids

    def generate_key_pair(self):
        ec2_keypair = self.ec2_boto3_client.create_key_pair(KeyName=self.ec2_key_name)
        ec2_priv_key = ec2_keypair["KeyMaterial"]
        self.logger.info(ec2_priv_key)

        with open(self.ssh_key_path, "w+") as f:
            f.write(ec2_priv_key)
        self.ssh_key = ec2_priv_key
        return ec2_priv_key

    def create_ec2_instance(self):
        #
        # start EC2 instance
        # note that we can start muliple instances at the same time
        #
        instances = self.ec2_resource_manager.create_instances(
            ImageId=self.aws_ami_image,
            MinCount=1,
            MaxCount=1,
            InstanceType=self.ec2_instance_type,
            KeyName=self.ec2_key_name,
            SecurityGroupIds=self.ec2_security_group_ids,
            BlockDeviceMappings=[
                {
                    "DeviceName": "/dev/sda1",
                    "Ebs": {
                        "VolumeSize": self.ec2_instance_disk_size,
                        "VolumeType": "standard",
                    },
                }
            ],
        )

        self.logger.info("Have created the instance: ", instances)
        self.logger.info("type: " + self.ec2_instance_type)
        instance = instances[0]
        # use the boto3 waiter
        self.logger.info("wait for launching to finish")
        instance.wait_until_running()
        self.logger.info("launch finished")
        # reload instance object
        instance.reload()
        self.ec2_instance = instance
        self.public_ip = instance.public_ip_address
        while not self.public_ip:
            instance.reload()
            self.logger.info("waiting for launching to finish")
            self.public_ip = instance.public_ip_address
        self.logger.warn("EC2 instance is created with ip address: " + self.public_ip)
        return instance