# Private Auto-DJ Mix Sets

Private mixes use the normal WordPress login. The bridge checks both the
job owner and the Mix-post author before generating a short-lived signed
download URL. The WordPress post has status private. Guests and other members
cannot obtain a new signed URL or see it in the public mix directory.

## Storage separation

The worker writes public audio to /output (host
/srv/dancecore/media/generated-mixes) and private audio to /private-output
(host /srv/dancecore/private-mixes). Never put /private-output inside the
public media root. The Nginx media-origin container mounts the latter at
/private:ro, separate from /data:ro.

The origin requires the following location before the public /dc-media/ alias:

    location ^~ /dc-media/private-mixes/ {
        secure_link $arg_sig,$arg_expires;
        secure_link_md5 "$secure_link_expires$uri PRIVATE_SIGNING_KEY";
        if ($secure_link = "") { return 403; }
        if ($secure_link = "0") { return 410; }
        alias /private/;
        autoindex off;
        limit_except GET { deny all; }
        add_header Cache-Control "private, no-store" always;
        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Robots-Tag "noindex, nofollow" always;
    }

PRIVATE_SIGNING_KEY is lowercase hex SHA256 of the concatenation of
(lowercase hex SHA256(worker_token)) and the literal string
"dcmix-private-url-v1". Keep the key in the private server config, never in
Git. WordPress computes the same key from the worker-token SHA256 verifier.
Apply the origin quota gate to this location too.

On Syburg, the private directory is owned by UID 10001, GID 101, mode
2750. Private MP3s are group-readable by the origin (mode 0640).

The cleanup timer checks authenticated deletion tombstones once a minute,
verifies SHA256, and removes either the public or private file without
touching original tracks. The janitor can run without any incoming port.

## Security characteristics

A signed origin URL expires after 10 minutes. It supports HTTP Range for
seeking and downloading without PHP proxying the audio bytes. **Someone who
obtains that signed URL can reuse/share it until expiration**, so this is
login-gated temporary access, not DRM or a per-byte login check. Bare paths,
expired signatures, and forged signatures cannot retrieve audio. The private
output is never staged or stored in the public document root.

## End-to-end validation

1. Confirm a private job creates a WordPress Mix post with status private,
   owned by the submitting member. The attachment source URL should be an
   admin-post.php action, never a bare storage URL.
2. Confirm unsigned /dc-media/private-mixes/<uuid>.mp3 gives HTTP 403, a
   logged-out admin-post request gives 401, expired signatures give 410,
   and a signed Range request gives 206.
3. Confirm the private post does not appear in /mixes/ for anonymous users.
4. Trash a test private Mix post, run the janitor, confirm the private file
   disappears and the deleted job is acknowledged. Public mixes and original
   track recordings must remain playable.