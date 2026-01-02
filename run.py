import yaml
import paramiko
import re
import json
import os
import logging
import sys
import requests
import urllib.parse

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
                server = attributes.get('server', '')
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

    logger.debug(f"Reservations for {slave_host}:\n{json.dumps(slave_reservations, indent=4)}")

    for server, master_server_reservations in master_reservations.items():
        if server in slave_reservations or server == "":
            slave_server_reservations = slave_reservations[server] if server in slave_reservations else {}

            match_server = '' if server == '' else f'server="{server}"'

            for ip, master_res in master_server_reservations.items():
                master_mac = master_res["attributes"]["mac-address"]

                conflict_ip = next(
                    (slave_ip for slave_ip, slave_res in slave_server_reservations.items()
                     if slave_res["attributes"]["mac-address"] == master_mac),
                    None
                )

                if ip not in slave_server_reservations:
                    if conflict_ip:
                        logger.warning(
                            f"Conflict found for MAC {master_mac} on {slave_host}: "
                            f"slave has IP {conflict_ip}, master has IP {ip}. Removing conflicting reservation."
                        )
                        remove_command = f'/ip dhcp-server lease remove [find {match_server} mac-address={master_mac}]'
                        ssh_command(client, remove_command)
                    logger.info(f"Adding reservation on {slave_host} for server {server} and IP {ip}: {master_res['raw']}")
                    add_command = f'/ip dhcp-server lease add ' + master_res["raw"]
                    ssh_command(client, add_command)
                else:
                    if master_res["raw"] != slave_server_reservations[ip]["raw"]:
                        if conflict_ip:
                            logger.warning(
                                f"Conflict found for MAC {master_mac} on {slave_host}: "
                                f"slave has IP {conflict_ip}, master has IP {ip}. Removing conflicting reservation."
                            )
                            remove_command = f'/ip dhcp-server lease remove [find {match_server} mac-address={master_mac}]'
                            ssh_command(client, remove_command)
                        logger.info(f"Replacing reservation on {slave_host} for server {server} and IP {ip}: {master_res['raw']}")
                        remove_command = f'/ip dhcp-server lease remove [find {match_server} address={ip}]'
                        ssh_command(client, remove_command)
                        add_command = f'/ip dhcp-server lease add ' + master_res["raw"]
                        ssh_command(client, add_command)
        else:
            logger.debug(f"Server {server} not found on {slave_host}. Skipping synchronization.")

            if slave_host not in missing_servers:
                missing_servers[slave_host] = []

            missing_servers[slave_host].append(server)

def sync_watchyourlan(master_reservations, wyl_config):
    wyl_url = wyl_config.get('url')
    if not wyl_url:
        logger.warning("WatchYourLAN URL not configured, skipping.")
        return

    logger.info(f"Syncing to WatchYourLAN at {wyl_url}...")

    try:
        # Fetch all hosts from WatchYourLAN
        response = requests.get(f"{wyl_url}/api/all")
        response.raise_for_status()
        wyl_hosts = response.json()
    except Exception as e:
        logger.error(f"Failed to fetch hosts from WatchYourLAN: {e}")
        return

    # Map MAC to Wyl Host
    # Normalize MACs to lowercase
    wyl_hosts_by_mac = {h['Mac'].lower(): h for h in wyl_hosts if 'Mac' in h}
    
    # Collect all master MACs
    master_macs = set()
    
    # Iterate through master reservations to update WYL
    for server, server_reservations in master_reservations.items():
        for ip, res in server_reservations.items():
            mac = res["attributes"].get("mac-address")
            if not mac:
                continue
            
            mac = mac.lower()
            master_macs.add(mac)
            
            if mac in wyl_hosts_by_mac:
                wyl_host = wyl_hosts_by_mac[mac]
                wyl_id = wyl_host['ID']
                
                # Get comment/name from Mikrotik
                name = res["attributes"].get("comment", "")
                
                current_known = wyl_host.get('Known', False)
                current_name = wyl_host.get('Name', '')

                # Logic: 
                # 1. Update name if different (using URL without /toggle)
                # 2. Toggle 'known' if state is wrong (using URL with /toggle)
                
                enc_name = urllib.parse.quote(name) if name else ""
                if not enc_name:
                     enc_name = "-"

                try:
                    # Step 1: Update Name if needed
                    if current_name != name:
                        # Call WITHOUT /toggle to just update name/id
                        update_url = f"{wyl_url}/api/edit/{wyl_id}/{enc_name}" 
                        requests.get(update_url)
                        logger.debug(f"Updated WatchYourLAN host {mac} name to '{name}'")
                    
                    # Step 2: Ensure Known=True
                    if not current_known:
                        # Toggle to True
                        toggle_url = f"{wyl_url}/api/edit/{wyl_id}/{enc_name}/toggle"
                        requests.get(toggle_url)
                        logger.info(f"Updated WatchYourLAN host {mac}: Set Known=True")
                        
                except Exception as e:
                    logger.error(f"Failed to update WatchYourLAN host {mac}: {e}")

    # Unmark known for hosts not in master
    for wyl_host in wyl_hosts:
        mac = wyl_host.get('Mac')
        if mac:
             mac = mac.lower()
             
        if mac and mac not in master_macs:
            if wyl_host.get('Known'): # If currently known
                try:
                    wyl_id = wyl_host['ID']
                    current_name = wyl_host.get('Name', 'Unknown')
                    enc_name = urllib.parse.quote(current_name)
                    if not enc_name:
                        enc_name = "-"

                    # Current=True. Target=False.
                    # One call toggles to False.
                    edit_url = f"{wyl_url}/api/edit/{wyl_id}/{enc_name}/toggle"
                    requests.get(edit_url)
                    logger.info(f"Unmarked known flag for WatchYourLAN host {mac} (not in master)")
                except Exception as e:
                    logger.error(f"Failed to unmark WatchYourLAN host {mac}: {e}")

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


    if config.get('watchyourlan'):
        wyl_configs = config.get('watchyourlan')
        if isinstance(wyl_configs, list):
            for wyl_config in wyl_configs:
                sync_watchyourlan(master_reservations, wyl_config)
        else:
             # Support legacy single object if user provided (though plan said list, robustness good)
             sync_watchyourlan(master_reservations, wyl_configs)


    logger.info("Reservation sync completed successfully.")
    sys.exit(0)

if __name__ == "__main__":
    main()
