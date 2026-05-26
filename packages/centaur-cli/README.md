# Centaur CLI

`centaur` is the guided setup CLI for Centaur. It scaffolds an overlay, records resumable onboarding state, generates integration templates, validates local prerequisites, and prints exact repair steps for Slack, model, GitHub, secrets, and deployment setup.

```bash
uv run centaur init --non-interactive --org acme --assistant-name centaur --domain centaur.acme.com
uv run centaur doctor --deep
uv run centaur integrations slack-manifest --domain centaur.acme.com
uv run centaur deploy kind
```

The wizard is safe by default. It writes local files and templates, but does not create remote Slack apps, GitHub apps, password-manager entries, or Kubernetes resources unless a later deploy command is run explicitly.
