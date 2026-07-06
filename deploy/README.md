# DTA Server Deployment

This repo is a standalone static website. It can live on the same Aliyun ECS server as the Amazon tools.

If there is no domain name yet, serve it under a separate IP path such as `/dta/`.

If there is a domain or subdomain later, serve it through a separate Nginx server block.

## Server Layout

- Site directory: `/var/www/dta`
- Git repository: `https://github.com/withMetta13/dta.git`
- IP-only URL: `http://8.163.43.107/dta/`
- Future domain URL: `http://dta.example.com/`

## First-Time Deployment With IP Only

On the Aliyun ECS server:

```bash
sudo mkdir -p /var/www
sudo git clone https://github.com/withMetta13/dta.git /var/www/dta
cd /var/www/dta
sudo DTA_PUBLIC_PATH=/dta ./deploy/server-setup.sh
```

Then open:

```text
http://8.163.43.107/dta/
```

## Updating Later

```bash
cd /var/www/dta
sudo git pull
sudo DTA_PUBLIC_PATH=/dta ./deploy/server-setup.sh
```

## Future Domain Deployment

After a real subdomain points to the ECS public IP:

```bash
cd /var/www/dta
sudo DTA_DOMAIN=dta.example.com ./deploy/server-setup.sh
```

## DNS

Create an A record for the chosen subdomain pointing to the Aliyun ECS public IP.

Example:

- Host: `dta`
- Type: `A`
- Value: ECS public IP
