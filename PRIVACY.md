# Privacy

The project has no telemetry service and does not upload configuration.

Model requests are transmitted to either the fixed official subscription
endpoint or the custom provider selected by the model alias. The selected custom
provider receives the request content and its own configured API key.

Local state under `%LOCALAPPDATA%\CodexRouter` includes provider URLs, model
identifiers, DPAPI-encrypted keys, the random local access capability, model
catalogs, integration receipts, and configuration backups.

Request records contain timestamps, provider/model identifiers, response status,
transport, and duration. The router does not deliberately log conversation bodies
or credential headers. Client and upstream software may have their own logging
policies. Runtime startup errors can appear in `service-boot.log`.

The source repository and release packages contain none of that runtime state.
Uninstalling the plugin does not erase it automatically because the integration
receipt may be needed for recovery. Disable routing before deleting local data.
