import logging
import os
import platform
import sys
from pprint import pprint

import docker

logging.basicConfig(level=logging.INFO)

logging.debug("Create Docker client")
client = docker.from_env()
logging.info("Created Docker client")

# Define network settings
subnet = '10.10.10.0/24'
gateway_ip = '10.10.10.1'
network_name = 'ansible_network'

# Define container settings
image_name = 'debian'
image_name_cnc = 'alpine'
container_count = 3
if platform.machine() == 'arm64':
    platform = 'linux/arm64'
else:
    platform = 'linux/amd64'

start_command = 'tail -f /dev/null'
cwd = os.getcwd()


def create_network():
    # Create network
    networks = client.networks.list(names=[network_name])
    if not networks:

        ipam_pool = docker.types.IPAMPool(subnet=subnet,
                                          iprange=subnet,
                                          gateway=gateway_ip)

        ipam_config = docker.types.IPAMConfig(pool_configs=[ipam_pool])
        client.networks.create(network_name, driver='bridge', ipam=ipam_config)
        logging.info("Created network")
    else:
        logging.warning("Network already exists")


def build_ansible_image():
    # Build image
    if not client.images.list(name=image_name_cnc):
        logging.debug(f"Pulling {image_name_cnc} image...")
        client.images.pull(image_name_cnc, platform=platform)
        logging.debug(f"Pulled {image_name_cnc}image")
    else:
        logging.debug(f"{image_name_cnc} image already exists")

    client.images.build(path='.',
                        dockerfile='Dockerfile-ansible-runner',
                        tag='my-ansible-runner')
    logging.info("Built ansible-runner image")


def build_debian_image():
    # Build image
    if not client.images.list(name=image_name):
        logging.debug(f"Pulling {image_name} image...")
        client.images.pull(image_name, platform=platform)
        logging.debug(f"Pulled {image_name} image")
    else:
        logging.debug(f"{image_name} image already exists")
    client.images.build(path='.',
                        dockerfile='Dockerfile-debian',
                        tag='my-debian')
    logging.info(f"Built {image_name} image")


def create_cnc_machine() -> docker.models.containers.Container:
    # Create containers

    container_name = f'cnc_machine'
    container = client.containers.create(image='my-ansible-runner',
                                         name=container_name,
                                         network=network_name,
                                         platform=platform,
                                         command=start_command,
                                         volumes={
                                             f'{cwd}/ansible': {
                                                 'bind': '/root/ansible',
                                                 'mode': 'rw'
                                             },
                                             f'{cwd}/specs': {
                                                 'bind': '/root/specs/',
                                                 'mode': 'rw'
                                             },
                                             f'{cwd}/ansible/ansible.cfg': {
                                                 'bind': '/root/.ansible.cfg',
                                                 'mode': 'rw'
                                             }
                                         },
                                         detach=True,
                                         dns=['8.8.8.8', '8.8.4.4'])

    logging.debug(f'Created container {container_name}')

    container.start()
    container.reload()

    command = 'chmod 600 /root/ansible/ansible_ed25519'
    container.exec_run(command)

    logging.info(
        f'Started container {container_name} ' +
        f'with IP address {container.attrs["NetworkSettings"]["Networks"][network_name]["IPAddress"]}'
    )
    return container


def create_containers() -> list[docker.models.containers.Container]:
    # Create containers
    containers = []
    for i in range(container_count):
        container_name = f'container_{i + 2}'
        container = client.containers.create(
            image=f'my-debian',
            name=container_name,
            network=network_name,
            platform=platform,
            command=start_command,
            volumes={
                f'{cwd}/ansible/keys': {
                    'bind': '/root/.ssh/',
                    'mode': 'rw'
                },
                f'{cwd}/ansible/fuzzed_playbooks': {
                    'bind': '/root/fuzzed_playbooks',
                    'mode': 'rw'
                },
                f'{cwd}/specs': {
                    'bind': '/root/specs/',
                    'mode': 'rw'
                },
                f'{cwd}/ansible/sshd_config.d': {
                    'bind': '/etc/ssh/sshd_config.d/',
                    'mode': 'rw'
                }
            },
            detach=True,
            dns=['8.8.8.8', '8.8.4.4'])

        logging.debug(f'Created container {container_name}')

        container.start()
        container.reload()
        logging.info(
            f'Started container {container_name} ' +
            f'with IP address {container.attrs["NetworkSettings"]["Networks"][network_name]["IPAddress"]}'
        )

        command = 'service ssh start'
        container.exec_run(command)

        logging.debug(f'Executed commands on container {container_name}')
        containers.append(container)

    return containers


def reset_containers(containers: list[docker.models.containers.Container]) -> list[docker.models.containers.Container]:
    logging.info(f'Resetting containers...')
    for cont in containers:
        cont.remove(force=True)
        logging.info(f'Removed container {cont.name}')
    return create_containers()


def delete_containers_and_network(signal=None, frame=None):
    # Remove containers
    for container in client.containers.list():
        container.remove(force=True)

        logging.info(f"Removed container {container.name}")

    # Remove network
    client.networks.get(network_name).remove()
    logging.info("Removed network")
    try:
        os.remove('specs/inverse_lock')
    except FileNotFoundError:
        pass
    sys.exit(0)


def exec_run_wrapper(cnc: docker.models.containers.Container, command: str) -> tuple[int, str]:
    res = cnc.exec_run(command)
    output: str = res.output.decode('utf-8')
    if res.exit_code == 0:
        logging.info(f"Executed command\n\t\t\t{command}\n\t\t\tsuccessfully")
        logging.debug(output)
    else:
        logging.error(f"Command {command} failed - code {res.exit_code}")
        logging.error(output)

    return res.exit_code, output


def run_ansible_playbook(playbook_path: str, cnc: docker.models.containers.Container) -> int:
    logging.info("Running Ansible playbook...")

    syntax_check = f'ansible-playbook --syntax-check  -i /root/ansible/inventory.ini /root/ansible/{playbook_path}'
    ret_code, _ = exec_run_wrapper(cnc, syntax_check)
    if ret_code != 0:
        logging.error(f"FAILED SYNTAX CHECK. ABORTING...")
        return ret_code
    else:
        logging.debug(f"SYNTAX CHECK OK. RUNNING PLAYBOOK...")

    actual_run = f'ansible-playbook -i /root/ansible/inventory.ini /root/ansible/{playbook_path}'
    ret_code, output = exec_run_wrapper(cnc, actual_run)
    if ret_code != 0:
        logging.error(f"FAILED PLAYBOOK RUN. ABORTING...")
        if 'MODULE FAILURE' in output:
            logging.error(f"MODULE FAILURE DETECTED.")
            ret_code = -1
        if 'Error while setting attributes' in output:
            pprint(output.split('chattr failed')[1].split('path')[1].split(',')[0].strip())
            ret_code = -2
    else:
        logging.debug(f"PLAYBOOK RUN OK.")
    return ret_code


def setup_infrastructure():
    create_network()
    build_ansible_image()
    build_debian_image()
    containers = create_containers()
    cnc_machine = create_cnc_machine()

    return containers, cnc_machine


if __name__ == '__main__':
    print("This file is not meant to be run directly.")
    print("Please run the main.py file instead.")
