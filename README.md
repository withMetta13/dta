# DTA XHS Checklist

Collaborative website for the Xiaohongshu baobei note optimization checklist. It records each submitted checklist in a server-side SQLite database.

Entry file:

- `index.html`

Deployment:

- This project is intentionally standalone so it can be connected to Alibaba Cloud with an independent domain or subdomain.
- Server deployment helper: `deploy/server-setup.sh`
- Recommended server directory: `/var/www/dta`

If you only have an ECS IP address, deploy under a separate path:

```bash
sudo DTA_PUBLIC_PATH=/dta ./deploy/server-setup.sh
```

The site will be available at:

```text
http://8.163.43.107/dta/
```

If you later bind a real domain or subdomain, deploy as a separate Nginx site:

```bash
sudo DTA_DOMAIN=dta.example.com ./deploy/server-setup.sh
```

Replace `dta.example.com` with the real subdomain after its DNS A record points to the Aliyun ECS public IP.
