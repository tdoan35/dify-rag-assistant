# Dify Platform Guide: Workspaces, Model Providers, Knowledge Pipelines, and Billing

This excerpt is adapted from Dify's own product documentation for support agents and the support-ticket assistant's knowledge base. It covers four areas: workspace and API key setup, connecting a model provider, building a basic RAG pipeline (Knowledge Pipeline), and billing/plan limits on Dify Cloud.

## 1. Workspace and API Key Setup

Every Dify Cloud account belongs to at least one **workspace**, a shared container for apps, knowledge bases, team members, and billing. The person who creates a workspace becomes its owner; owners and admins can invite additional members from **Settings → Members**, where new invitees are assigned a role (Owner, Admin, Editor, or Normal member). Each workspace plan caps how many members can be active at once (see Section 4); if a workspace is downgraded below its current member count, the members over the new cap lose workspace access immediately until seats are freed or the plan is upgraded again.

Dify issues two different kinds of API keys, and they are **not interchangeable**:

- **App API keys** authenticate calls to a specific published app (Chatbot, Agent, Workflow, or Chatflow). They are created from that app's own **API Access** (sometimes shown as "Monitoring → API Key") panel inside the app builder, by clicking **Create new Secret Key**. The key is shown once, prefixed `app-`, and must be stored by the developer immediately since Dify does not redisplay it later. An app's API key only works for that one app — a key created for one chatbot will return an authentication error if used against a different app's endpoint, even inside the same workspace.
- **Dataset (knowledge base) API keys** authenticate calls to the Knowledge/dataset API — creating datasets, uploading documents, or querying a knowledge base directly without going through a published app. These are created from the dataset's own detail page, under its **Service API** panel, and are scoped only to the knowledge base(s) they were created against. A dataset key will not authenticate a request to an app endpoint, and an app key will not authenticate a dataset-API request.

A fresh key that returns a `401 Unauthorized` almost always means one of: the wrong key type was used for the endpoint being called (app key vs. dataset key), the key was pasted with surrounding whitespace, or the app/dataset the key belongs to has since been deleted or unpublished. Rotating a compromised key is done by creating a new secret key and deleting the old one from the same panel; there is no separate "rotate" action.

## 2. Connecting a Model Provider

Dify apps need at least one language model to run. Workspaces start with access to a small set of **Dify-hosted models** (from OpenAI, Anthropic, Gemini, xAI, and Tongyi) paid for out of the workspace's message-credit balance (Section 4). To move beyond the included credits, or to use a provider not offered on Dify-hosted credits, a workspace connects its **own** model provider:

1. Go to **Settings → Model Provider**, or open the **Marketplace** and filter by "Models."
2. Install the plugin for the desired provider (OpenAI, Anthropic, Azure OpenAI, and others are published officially; additional community providers are also listed).
3. Enter the credentials the provider requires — typically an API key, and for Azure OpenAI, also the resource endpoint URL and deployment name.
4. Save, then set that provider's model as the default (or per-app override) under the app's **Model** settings.

Once a workspace supplies its own key for a provider, calls to that provider's models are billed directly by the provider and no longer draw down the workspace's Dify message credits. Message credits and bring-your-own-key billing can be mixed per app: one app can run on Dify-hosted credits while another, in the same workspace, runs on a connected OpenAI key.

Message credits are not consumed at a flat rate. Each model response is metered in credits according to the specific model used — lighter models (for example smaller GPT variants) consume as little as 1 credit per response, mid-tier models around 5 credits, and larger or multimodal models (audio transcription, text-to-speech, flagship chat models) around 10 credits per response. A workspace that mostly uses a heavier model will exhaust its credit balance noticeably faster than the headline credit number suggests. Once a workspace's credits are used up, model calls on Dify-hosted credits stop working until the workspace either connects its own API key for that provider or waits for the next monthly credit reset (paid plans) — Sandbox credits do not reset.

Model providers unrelated to standard chat/completion — for example self-hosted or local model runtimes — are managed the same way, through a provider plugin in the Marketplace, if a published plugin exists for that runtime; this guide does not enumerate which self-hosted runtimes currently have an official plugin.

## 3. Building a Basic RAG Pipeline (Knowledge Pipeline)

A Knowledge Pipeline turns uploaded files or connected data sources into a searchable **dataset** that an app's retrieval node can query. Every document a pipeline ingests passes through four stages, in order: **Parse → Enrich → Chunk → Embed/Index**.

- **Parse** extracts raw text from the source file (PDF, DOCX, Markdown, website, Notion page, etc.).
- **Enrich** applies optional cleanup steps, such as stripping boilerplate or extracting tables.
- **Chunk** splits the parsed text into retrievable units, using one of three chunking modes chosen when the dataset is created or a document is added: **General** (fixed-size segments with configurable overlap), **Parent-Child** (a small child chunk is embedded and matched, but its larger parent chunk is returned to the model for fuller context), or **Q&A** (the pipeline generates question/answer pairs from the source text, useful for FAQ-style content).
- **Embed/Index** converts each chunk into a retrievable record, using one of two indexing techniques: **Economical** (keyword-based index, free, does not count against the workspace's Knowledge Data Storage quota) or **High Quality** (vector embeddings via the workspace's embedding model, which does count against the plan's Knowledge Data Storage quota and stops accepting new documents once that quota is full).

When a document is edited and saved, it is re-processed through these same four stages before its chunks are available for retrieval again; a newly indexed document is not searchable mid-processing.

Before publishing an app that uses a dataset, the **Hit Testing** / retrieval-test panel on the dataset page lets a builder type a sample query and see exactly which chunks would be retrieved and their similarity scores, without needing to run the full app. Retrieval behavior in a live app is governed by settings on the retrieval node: **top_k** (how many chunks to return), an optional **score threshold** (chunks scoring below it are discarded even if they would otherwise be in the top_k), and whether **reranking** is enabled via a connected rerank model. A retrieval step that returns no results is most often explained by the score threshold being set too high, the document still being mid-indexing, or the query simply not matching any ingested content — not by an account or provider-propagation delay.

Each workspace plan caps the number of knowledge documents and the Knowledge Data Storage a dataset can consume; see Section 4. A request that would push either over the plan's cap is rejected at upload time with an error naming the limit, rather than silently truncating the document set.

## 4. Billing, Plans, and Limits

Dify Cloud is billed per **workspace**, not per individual seat, across four tiers. Monthly and annual billing are both offered; annual billing is discounted roughly 17% versus paying monthly.

| Plan | Price | Message credits | Team members | Apps | Knowledge documents | Knowledge data storage | API rate limit |
|---|---|---|---|---|---|---|---|
| Sandbox | Free | 200 credits, one-time (does not renew) | 1 | 5 | 50 | 50 MB | 5,000 calls/month |
| Professional | $59/workspace/month ($590/year) | 5,000 credits/month | 3 | 50 | 500 | 5 GB | No Dify-imposed API rate limit |
| Team | $159/workspace/month ($1,590/year) | 10,000 credits/month | 50 | 200 | 1,000 | 20 GB | No Dify-imposed API rate limit |
| Enterprise | Custom (contact sales) | Custom | Custom | Custom | Custom | Custom | Custom |

Only the workspace **owner** and **admins** can open the Billing page and manage a subscription. Payment is by credit card by default; paying by invoice or bank transfer is still being evaluated by Dify and, if it ships, is expected to be limited to annual Team subscriptions only — it is not available on any plan today. If a workspace's usage (documents, storage, or team members) exceeds the limits of a lower plan after a downgrade, the resources or members above the new plan's cap become inaccessible rather than being deleted; restoring access requires upgrading again or reducing usage back under the new plan's limits.

Enterprise is a separately sold, typically self-hosted or VPC-deployed product line for organizations that need capabilities beyond what any Cloud tier offers, such as single sign-on, detailed audit logging, and a dedicated customer success manager; Enterprise pricing and feature scope are negotiated directly with Dify's sales team rather than published, and this guide does not detail Enterprise configuration.

## What this guide does not cover

This excerpt focuses on day-to-day workspace, model-provider, Knowledge Pipeline, and Cloud-billing questions. It does not document Enterprise-only configuration (SSO/SAML setup, audit-log export formats, compliance certifications), self-hosted/Community Edition deployment, the plugin SDK for building custom integrations, or workflow-level orchestration features beyond the retrieval node settings described above. Questions in those areas should be routed to the appropriate specialist team rather than answered from this material alone.
