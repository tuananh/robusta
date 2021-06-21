import time
from typing import Type, TypeVar, List, Dict

import hikaru
import json
import yaml
from hikaru.model import *
from pydantic import BaseModel

from .api_client_utils import *
from .templates import get_deployment_yaml

S = TypeVar("S")
T = TypeVar("T")
PYTHON_DEBUGGER_IMAGE = "us-central1-docker.pkg.dev/arabica-300319/devel/python-tools:latest"


# TODO: import these from the lookup_pid project
class Process(BaseModel):
    pid: int
    exe: str
    cmdline: List[str]


class ProcessList(BaseModel):
    processes: List[Process]


def get_images(containers: List[Container]) -> Dict[str, str]:
    """
    Takes a list of containers and returns a dict mapping image name to image tag.
    """
    name_to_version = {}
    for container in containers:
        if ":" in container.image:
            image_name, image_tag = container.image.split(":", maxsplit=1)
            name_to_version[image_name] = image_tag
        else:
            name_to_version[container.image] = "<NONE>"
    return name_to_version


class RobustaPod(Pod):

    def exec(self, shell_command: str) -> str:
        """Execute a command inside the pod"""
        return exec_shell_command(self.metadata.name, shell_command, self.metadata.namespace)

    def get_logs(self, container_name=None, previous=None, tail_lines=None) -> str:
        """
        Fetch pod logs
        """
        if container_name is None:
            container_name = self.spec.containers[0].name
        return get_pod_logs(self.metadata.name, self.metadata.namespace, container_name, previous, tail_lines)

    def create_debugger_pod(self, debug_image=PYTHON_DEBUGGER_IMAGE, debug_cmd=None) -> 'RobustaPod':
        """
        Creates a debugging pod with high privileges
        """
        debugger = RobustaPod(apiVersion="v1", kind="Pod",
                              metadata=ObjectMeta(name=to_kubernetes_name(self.metadata.name, "debug-"),
                                                  namespace="robusta"),
                              spec=PodSpec(hostPID=True,
                                           nodeName=self.spec.nodeName,
                                           containers=[Container(name="debugger",
                                                                 image=debug_image,
                                                                 imagePullPolicy="Always",
                                                                 command=prepare_pod_command(debug_cmd),
                                                                 securityContext=SecurityContext(
                                                                     capabilities=Capabilities(
                                                                         add=["SYS_PTRACE", "SYS_ADMIN"]
                                                                     ),
                                                                     privileged=True
                                                                 ))]))
        # TODO: check the result code
        debugger = debugger.createNamespacedPod(debugger.metadata.namespace).obj
        return debugger

    def exec_in_debugger_pod(self, cmd, debug_image=PYTHON_DEBUGGER_IMAGE) -> str:
        debugger = self.create_debugger_pod(debug_image)
        try:
            return debugger.exec(cmd)
        finally:
            RobustaPod.deleteNamespacedPod(debugger.metadata.name, debugger.metadata.namespace)

    def get_processes(self) -> List[Process]:
        output = self.exec_in_debugger_pod(f"/lookup_pid.py {self.metadata.uid}")
        # somehow when doing the exec command the quotes in the json output are converted from " to '
        # we fix this so that we can deserialize the json properly...
        # we should eventually figure out why this is happening
        output = output.replace("'", '"')
        processes = ProcessList(**json.loads(output))
        return processes.processes

    def get_images(self) -> Dict[str, str]:
        return get_images(self.spec.containers)

    @staticmethod
    def find_pod(name_prefix, namespace) -> 'RobustaPod':
        pods: PodList = PodList.listNamespacedPod(namespace).obj
        for pod in pods.items:
            if pod.metadata.name.startswith(name_prefix):
                # we serialize and then deserialize to work around https://github.com/haxsaw/hikaru/issues/15
                return hikaru.from_dict(pod.to_dict(), cls=RobustaPod)
        raise Exception(f"No pod exists in namespace '{namespace}' with name prefix '{name_prefix}'")

    @staticmethod
    def read(name: str, namespace: str) -> 'RobustaPod':
        """Read pod definition from the API server"""
        return Pod.readNamespacedPod(name, namespace).obj


class RobustaDeployment(Deployment):

    @classmethod
    def from_image(cls: Type[T], name, image="busybox", cmd=None) -> T:
        obj: RobustaDeployment = hikaru.from_dict(yaml.safe_load(get_deployment_yaml(name, image)), RobustaDeployment)
        obj.spec.template.spec.containers[0].command = prepare_pod_command(cmd)
        return obj

    def get_images(self) -> Dict[str, str]:
        return get_images(self.spec.template.spec.containers)


class RobustaJob(Job):

    def get_pods(self) -> List[RobustaPod]:
        """
        gets the pods associated with a job
        """
        pods: PodList = PodList.listNamespacedPod(self.metadata.namespace,
                                                  label_selector=f"job-name = {self.metadata.name}").obj
        # we serialize and then deserialize to work around https://github.com/haxsaw/hikaru/issues/15
        return [hikaru.from_dict(pod.to_dict(), cls=RobustaPod) for pod in pods.items]

    def get_single_pod(self) -> RobustaPod:
        """
        like get_pods() but verifies that only one pod is associated with the job and returns that pod
        """
        pods = self.get_pods()
        if len(pods) != 1:
            raise Exception(f"got more pods than expected for job: {pods}")
        return pods[0]

    @classmethod
    def run_simple_job_spec(cls, spec, name, timeout) -> str:
        job = RobustaJob(metadata=ObjectMeta(namespace="robusta", name=to_kubernetes_name(name)),
                         spec=JobSpec(backoffLimit=0,
                                      template=PodTemplateSpec(
                                          spec=spec,
                                      )))
        try:
            job = job.createNamespacedJob(job.metadata.namespace).obj
            job = hikaru.from_dict(job.to_dict(), cls=RobustaJob)  # temporary workaround for hikaru bug #15
            job: RobustaJob = wait_until_job_complete(job, timeout)
            job = hikaru.from_dict(job.to_dict(), cls=RobustaJob)  # temporary workaround for hikaru bug #15
            pod = job.get_single_pod()
            return pod.get_logs()
        finally:
            job.deleteNamespacedJob(job.metadata.name, job.metadata.namespace, propagation_policy="Foreground")

    @classmethod
    def run_simple_job(cls, image, command, timeout) -> str:
        spec = PodSpec(
            containers=[Container(name=to_kubernetes_name(image),
                                  image=image,
                                  command=prepare_pod_command(command))],
            restartPolicy="Never"
        )
        return cls.run_simple_job_spec(spec, name=image, timeout=timeout)


hikaru.register_version_kind_class(RobustaPod, Pod.apiVersion, Pod.kind)
hikaru.register_version_kind_class(RobustaDeployment, Deployment.apiVersion, Deployment.kind)
hikaru.register_version_kind_class(RobustaJob, Job.apiVersion, Job.kind)