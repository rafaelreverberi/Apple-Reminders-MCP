# Security policy

## Boundaries and threats

Protected assets include the Apple password, 2FA codes, pyicloud session cookies,
keyring contents, Reminder data, locations, confirmation state, and the OpenAI
tunnel runtime key. Threats include a stolen Pi/session/keyring, unsafe `.env`
permissions, public MCP exposure, tunnel credential theft, replayed deletion,
malicious Reminder text/URLs, dependency compromise, excessive Apple requests,
and private CloudKit API changes.

The Apple password is entered only into pyicloud's interactive CLI on the Pi. No
MCP login tool exists. Passwords and 2FA codes are never environment variables,
arguments, source, logs, or chat input. Session files remain local and must be
treated like credentials. Use a secure keyring or session-only mode; plaintext
keyring backends are unsupported.

The MCP binds only to loopback and rejects other hosts. The OpenAI runtime creates
outbound HTTPS connectivity; never add port forwarding or a public proxy. Rotate
`CONTROL_PLANE_API_KEY` if exposed and check tunnel organization/workspace scope.

Reminder titles, notes, hashtags, attachment URLs, and location names are hostile
data. They never authorize tools or bypass allowlists. URLs are returned/attached
but never automatically opened or downloaded. Precise locations and Reminder
content must not be written to operational logs. Failure context uses one-way
hashed list/record references; exception tracebacks stay server-side and are never
included in MCP error payloads.

Writes are separately switchable and list-scoped. Exact IDs are mandatory. Delete
requires a short-lived HMAC token stored with atomic replay state; a changed remote
revision invalidates it. Parent deletion is blocked when subtasks are present.
Post-delete success additionally requires remote evidence that the record is
absent, carries Apple's deleted flag, or is no longer active in its original list;
a successful request dispatch alone is insufficient.
Rotate `CONFIRMATION_SIGNING_SECRET` to invalidate all outstanding tokens.

If the Pi is compromised: stop the service; revoke Apple web sessions at the Apple
Account security page; remove any keyring password; rotate the Apple password when
appropriate; rotate tunnel and confirmation keys; review logs; reinstall from a
trusted image. `./auth.sh logout` clears the selected local pyicloud session, but
remote revocation should also be performed for suspected theft.

Apple may require new terms or change CloudKit. The MCP never accepts terms and
does not turn unknown write outcomes into success. Keep dependencies locked,
review updates, run tests, and perform designated-list live validation before
trusting a new pyicloud version.

Report vulnerabilities privately with synthetic data. Never include credentials,
session files, Reminder content, attachment URLs, or coordinates in an issue.
