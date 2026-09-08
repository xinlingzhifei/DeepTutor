# Shared-host platform ingress

The current platform candidate publishes only its gateway, at
`127.0.0.1:13780 -> 80/tcp` and `127.0.0.1:13783 -> 443/tcp`.
These bindings are committed in `docker-compose.platform.yml` and covered by
the candidate's existing source/artifact binding. Do not add a Compose override
or expose an application/database port to make deployment pass.

The existing host reverse proxy continues to own public 80/443. Configure only
this application's virtual host using `yfeistai-shared-host.conf.example`:

1. Choose the real application hostname and replace **both** its `server_name`
   and `proxy_ssl_name` occurrences. The example `.invalid` name is not deployable.
2. Provision the host TLS certificate/key at the example's paths, and the
   gateway certificate/key through the existing read-only Compose secrets.
   Both certificates must validate for that hostname. Keep the proxy's upstream
   certificate verification enabled; for a private CA, use a separately managed
   trust bundle instead of disabling verification.
3. Verify the Docker Engine version and network/firewall configuration before
   deployment. Require Engine **28.0.0 or newer** for localhost port isolation;
   older engines have a documented same-L2 exposure issue. Do not enable direct
   container routing or unprotected gateway modes.
4. Check both loopback ports are available. Install only the dedicated virtual
   host, run the host's Nginx syntax check, and compare other projects' routes and
   container identities before/after any approved reload. Do not replace the
   global/default proxy configuration.
5. Obtain the existing runtime evidence using the exact new candidate. Gateway
   observation schema 2 additionally probes public 13780/13783 as **closed**.
   Schema 1 is retained in the report parser for historical direct-port evidence.
   Verify an old candidate with its own source revision: the current full-byte
   candidate validator deliberately rejects an old Compose template. Schema 1
   is never proof for a loopback candidate. The original twelve live gates remain
   required; local tests or a successful proxy syntax check are not live evidence.

The host proxy forwards HTTPS to the loopback gateway with SNI and certificate
verification. The gateway retains its existing authenticated application routes,
internal-route restrictions, caching rules, and TLS validation. No application
or storage service gains a public route. The outer proxy replaces an untrusted
incoming forwarding chain with the observed peer address.

This file is deployment guidance, not evidence that a server, TLS certificate,
proxy route, firewall, or runtime has been inspected or changed.

References: [Docker port publishing](https://docs.docker.com/engine/network/port-publishing/)
and [Nginx upstream TLS verification](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_ssl_verify).
