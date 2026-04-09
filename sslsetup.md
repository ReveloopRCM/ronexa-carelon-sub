# SSL Setup — carelon.ronexa.com

## Status: BLOCKED — waiting for private key

---

## What We Have

| Item | Details |
|------|---------|
| **Target URL** | `https://carelon.ronexa.com` |
| **Current URL** | `http://ronexa.centralus.cloudapp.azure.com` |
| **Orchestrator IP** | `20.29.73.195` (static) |
| **SSL Certificate** | Wildcard `*.ronexa.com` (GoDaddy) |
| **Cert file** | `5d540aabb744768e.crt` |
| **CA Bundle** | `gd_bundle-g2.crt` (GoDaddy intermediate chain) |
| **Valid** | Feb 10, 2026 → Feb 10, 2027 |
| **DNS Provider** | GoDaddy (`ns29.domaincontrol.com`) |
| **NSG Port 443** | Already open ✅ |

## MISSING: Private Key

The `.key` file generated when the CSR was created for GoDaddy. Typically named:
- `ronexa.com.key`
- `server.key`
- `_.ronexa.com.key`

**Where to look:**
- The machine where you generated the CSR (`openssl req -new -newkey ...`)
- GoDaddy account → SSL Certificates → Download → may have option to rekey
- If using cPanel/Plesk — check the SSL/TLS manager
- Email from when you originally set up the cert

**If the key is truly lost**, you can re-key the certificate through GoDaddy (generate a new CSR + key pair, submit CSR to GoDaddy, get new cert files).

---

## Implementation Steps (once private key is available)

### Step 1: DNS — Add A Record at GoDaddy

In GoDaddy DNS management for `ronexa.com`:
```
Type: A
Name: carelon
Value: 20.29.73.195
TTL: 600 (10 min)
```

Verify propagation:
```bash
dig +short carelon.ronexa.com
# Should return: 20.29.73.195
```

### Step 2: Prepare SSL Files on Orchestrator

```bash
# On your local machine — combine cert + CA bundle
cat 5d540aabb744768e.crt gd_bundle-g2.crt > fullchain.pem

# Copy to orchestrator
ssh ronexa@20.29.73.195 "mkdir -p /home/ronexa/ronexa/ssl"
scp fullchain.pem ronexa@20.29.73.195:/home/ronexa/ronexa/ssl/
scp YOUR_PRIVATE_KEY.key ronexa@20.29.73.195:/home/ronexa/ronexa/ssl/private.key

# Set permissions
ssh ronexa@20.29.73.195 "chmod 600 /home/ronexa/ronexa/ssl/private.key"
```

Verify cert + key match:
```bash
ssh ronexa@20.29.73.195 "
  openssl x509 -noout -modulus -in /home/ronexa/ronexa/ssl/fullchain.pem | md5sum
  openssl rsa -noout -modulus -in /home/ronexa/ronexa/ssl/private.key | md5sum
"
# Both MD5 hashes must match
```

### Step 3: Update Nginx Config

Create `/home/ronexa/ronexa/nginx.conf`:

```nginx
# Redirect HTTP → HTTPS
server {
    listen 80;
    server_name carelon.ronexa.com;
    return 301 https://$host$request_uri;
}

# Catch-all HTTP (old URL or direct IP) → redirect
server {
    listen 80 default_server;
    return 301 https://carelon.ronexa.com$request_uri;
}

# HTTPS
server {
    listen 443 ssl;
    server_name carelon.ronexa.com;

    ssl_certificate     /etc/nginx/ssl/fullchain.pem;
    ssl_certificate_key /etc/nginx/ssl/private.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # Security headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;

    # Frontend
    location / {
        proxy_pass http://frontend:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Backend API
    location /api/ {
        proxy_pass http://backend-api:8000/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 120s;
        proxy_read_timeout 120s;
    }
}
```

### Step 4: Update docker-compose.yml

```yaml
nginx:
    image: nginx:alpine
    container_name: nginx
    ports:
      - "80:80"
      - "443:443"    # ← ADD THIS
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - ./ssl:/etc/nginx/ssl:ro    # ← ADD THIS
    depends_on:
      - backend-api
      - frontend
    restart: always
```

### Step 5: Deploy

```bash
ssh ronexa@20.29.73.195 "cd /home/ronexa/ronexa && docker compose up -d nginx --force-recreate"
```

### Step 6: Verify

```bash
# Test HTTPS
curl -I https://carelon.ronexa.com

# Test HTTP redirect
curl -I http://carelon.ronexa.com
# Should return 301 → https://carelon.ronexa.com

# Test SSL cert
openssl s_client -connect carelon.ronexa.com:443 -servername carelon.ronexa.com < /dev/null 2>/dev/null | openssl x509 -noout -subject -dates
```

### Step 7: Update Frontend Environment (if needed)

If the frontend uses `NEXT_PUBLIC_API_URL`, update it:
```
NEXT_PUBLIC_API_URL=https://carelon.ronexa.com
```

The internal Docker network communication (frontend → backend-api) stays HTTP — only the nginx ↔ client connection uses SSL.

---

## Rollback

If anything goes wrong:
```bash
# Revert nginx to HTTP-only
ssh ronexa@20.29.73.195 "cd /home/ronexa/ronexa && docker compose up -d nginx --force-recreate"
```
The old `ronexa.centralus.cloudapp.azure.com` URL will keep working on HTTP.
