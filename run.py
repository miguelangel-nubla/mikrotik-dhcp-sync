import yaml
import paramiko
import re
import json
import os
import logging
import sys

config_dir = os.environ.get("CONFIG_DIR", "./")

log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

missing_servers = {}

def handle_error(message, exit_code=1):
    logger.error(message)
    sys.exit(exit_code)

def load_config(config_file):
    try:
        with open(config_file, 'r') as file:
            return yaml.safe_load(file)
    except Exception as e:
        handle_error(f"Error loading config file {config_file}: {e}")

def ssh_connect(host, username, password=None, key_file=None):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        if password:
            client.connect(host, username=username, password=password)
        elif key_file:
            client.connect(host, username=username, key_filename=key_file)
        else:
            raise ValueError("Either password or key_file must be provided")
    except Exception as e:
        handle_error(f"Error during SSH connection to {host}: {e}")

    return client

def ssh_command(client, command):
    try:
        stdin, stdout, stderr = client.exec_command(command)
        output = stdout.read().decode('utf-8')
        error = stderr.read().decode('utf-8')

        logger.debug(f"SSH Command: {command}")
        if output:
            logger.debug(f"SSH Command Output: {output}")
        if error:
            logger.error(f"SSH Command Error: {error}")

        return output
    except Exception as e:
        handle_error(f"Error during SSH command execution: {e}")

def get_dhcp_reservations(client, command='/ip dhcp-server export terse'):
    output = ssh_command(client, command)
    reservations = {}
    for line in output.splitlines():
        server_match = re.match(r'^/ip dhcp-server add\s+(.*)$', line)
        if server_match:
            attributes = parse_attributes(server_match.group(1))
            reservations[attributes['name']] = {}

        match = re.match(r'^/ip dhcp-server lease add\s+(.*)$', line)
        if match:
            raw = match.group(1)
            attributes = parse_attributes(raw)
            if 'address' in attributes:
                # handle special case no server means all
                server = attributes.get('server', "all")
                address = attributes['address']
                if server not in reservations:
                    reservations[server] = {}
                reservations[server][address] = {
                    "attributes": attributes,
                    "raw": raw
                }

    return reservations

def parse_attributes(line):
    attributes = {}
    tokens = re.findall(r'(\S+="[^"]*"|\S+)', line)
    for token in tokens:
        if '=' in token:
            key, value = token.split('=', 1)
            value = re.sub(r"\\(.)", r"\1", value.strip('"'))
            attributes[key] = value
    return attributes

def sync_reservations(master_reservations, slave_host, client):
    slave_reservations = get_dhcp_reservations(client)

    logger.debug("Reservations for {slave_host}:\n%s", json.dumps(slave_reservations, indent=4))

    for server, master_server_reservations in master_reservations.items():
        if server in slave_reservations:
            slave_server_reservations = slave_reservations[server]

            for ip, master_res in master_server_reservations.items():
                if ip not in slave_server_reservations:
                    logger.info(f"Adding reservation on {slave_host} for server {server} and IP {ip}: {master_res['raw']}")
                    command = f'/ip dhcp-server lease add ' + master_res["raw"]
                    ssh_command(client, command)
                else:
                    # can´t do a full check on decoded attributes because checkbox attribute parsing is not implemented
                    if master_res["raw"] != slave_server_reservations[ip]["raw"]:
                        server = master_res["attributes"]["server"]
                        # updating will not work here since attributes like block-access=yes will not be restored to default
                        logger.info(f"Replacing reservation on {slave_host} for server {server} and IP {ip}: {master_res['raw']}")
                        command = f'/ip dhcp-server lease remove [find server={server} address={ip}]'
                        ssh_command(client, command)
                        command = f'/ip dhcp-server lease add ' + master_res["raw"]
                        ssh_command(client, command)
        else:
            logger.debug(f"Server {server} not found on {slave_host}. Skipping synchronization.")

            if slave_host not in missing_servers:
                missing_servers[slave_host] = []

            missing_servers[slave_host].append(server)

def main():
    config_file = os.path.join(config_dir, "config.yaml")
    config = load_config(config_file)

    master_router = config.get("master")
    logger.info(f"Reading reservations from {master_router['host']}...")
    master_client = ssh_connect(master_router['host'], master_router['username'],
                                master_router.get('password'), master_router.get('key_file'))

    master_reservations = get_dhcp_reservations(master_client)
    logger.debug("Reservations for {master_router['host']}:\n%s", json.dumps(master_reservations, indent=4))

    if not master_reservations:
        handle_error("No reservations found on the master router.")

    for slave_router in config.get("slaves", []):
        logger.info(f"Syncing reservations to {slave_router['host']}...")
        slave_client = ssh_connect(slave_router['host'], slave_router['username'],
                                   slave_router.get('password'), slave_router.get('key_file'))
        sync_reservations(master_reservations, slave_router['host'], slave_client)

        slave_client.close()

    master_client.close()


    if missing_servers:
        for slave, servers in missing_servers.items():
            logger.warning(f"On slave {slave}, the following servers were missing: {', '.join(servers)}.")
        sys.exit(2)

    logger.info("Reservation sync completed successfully.")
    sys.exit(0)

if __name__ == "__main__":
    main()
