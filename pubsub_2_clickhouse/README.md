# Setting up the service

The PubSub to Clickhouse connector runs in a VM. To create this service, in GCP:

1. Create a service account. It will need the following roles:

- `` # TODO

User access within the service account needs to be configured as well.

2. Create a VM.

- e2-standard-2
- Standard provisioning
- 128 GB boot disk
- Add the corresponding service account
- No firewall rules since it won't receive external traffic
- No need to set up a static IP

3. Configure VM

- Install python and venv
    - `sudo apt-get update`
    - `sudo apt-get install python3`
    - `sudo apt-get install python3-venv -y`
- Create directory and venv
    - `sudo mkdir -p /opt/pubsub-2-clickhouse`
    - `cd /opt/pubsub-2-clickhouse/`
    - `sudo python3 -m venv venv`
- Create service user and change ownership
    - `sudo useradd -r -s /bin/false serviceuser`
    - `sudo chown -R serviceuser:serviceuser /opt/pubsub-2-clickhouse`
    - `sudo chmod -R 755 /opt/pubsub-2-clickhouse`

4. Configure the `deploy-compute-engine` GitHub action to point to the correct VM and run it to deploy `main.py` and the `.service` file.
