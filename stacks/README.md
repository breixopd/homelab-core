# Platform Compose resources

`platform.yaml` owns the fixed networks and named volumes shared by the deployment.

Every runtime service is a standalone Compose application at
`toolkit/services/<service>/compose.yaml`. A service folder may own tightly coupled
sidecars or mutually exclusive runtime variants. `homelab-toolkit generate` validates
and flattens those applications with the platform resources into `docker-compose.yml`.

To add or remove an application service, change only its service folder and catalog
configuration. The generated root file is never edited directly.
