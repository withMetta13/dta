# DTA XHS Checklist

Collaborative website for the Xiaohongshu baobei note optimization checklist. It records each submitted checklist in a server-side SQLite database.

Entry files:

- `index.html`: DTA 小红书工具导航
- `checklist/index.html`: 达人合作 Checklist
- `review-app/`: 自制潜力爆文审核台的最小运行包

Deployment:

- This project is intentionally standalone so it can be connected to Alibaba Cloud with an independent domain or subdomain.
- Server deployment helper: `deploy/server-setup.sh`
- Recommended server directory: `/var/www/dta`

If you only have an ECS IP address, deploy under a separate path:

```bash
sudo DTA_PUBLIC_PATH=/dta ./deploy/server-setup.sh
```

The navigation and checklist will be available at:

```text
http://8.163.43.107/dta/
http://8.163.43.107/dta/checklist/
http://8.163.43.107/dta/review/
```

更新采集数据时，部署脚本会用私有上传的 `review-app/deploy/incoming.sqlite3` 更新云端主库，同时保留云端已有的人工审核状态与备注。数据库、封面和共享报告被 `.gitignore` 排除，禁止推送到 GitHub；`/dta/review-app/` 也会被 Nginx 直接拒绝。

If you later bind a real domain or subdomain, deploy as a separate Nginx site:

```bash
sudo DTA_DOMAIN=dta.example.com ./deploy/server-setup.sh
```

Replace `dta.example.com` with the real subdomain after its DNS A record points to the Aliyun ECS public IP.
