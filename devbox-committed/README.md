# devbox

Ubuntu 22.04 container with SSH, Node.js, Python, and common dev tooling. Run on the NAS as a remote dev environment.

## Setup

From this directory on the NAS:

```bash
sudo docker compose up -d --build
sudo docker compose exec devbox passwd dev
```

Then SSH in:

```bash
ssh dev@<nas-domain> -p 2222
```
