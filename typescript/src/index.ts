/**
 * D2B TypeScript SDK — Spreadsheets for AI Agents.
 *
 * A thin, agent-native client over the D2B v1 API. Behaviour over
 * types: automatic Idempotency-Key on mutations, retry with backoff on
 * 429/5xx, problem+json errors with `suggestedFix` surfaced, a job
 * polling helper for async ingest, and webhook signature verification.
 *
 * ```ts
 * import { D2BClient } from "d2b-sdk";
 *
 * const client = new D2BClient({ apiKey: "d2b_pat_...", baseUrl: "https://d2b.dev" });
 * const wb = await client.workbooks.create({ title: "monthly" });
 * const result = await client.sources.upload(wb.id, file, { wait: true });
 * const tables = await client.tables.list(wb.id);
 * ```
 */

/**
 * The published version. Kept in step with package.json by
 * `test/version.test.ts` — a release bumps both or CI fails.
 */
export const VERSION = "0.1.0";

const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const RETRY_STATUSES = new Set([429, 502, 503, 504]);

export interface D2BClientOptions {
  apiKey: string;
  baseUrl: string;
  maxRetries?: number;
  fetch?: typeof fetch;
}

export class D2BError extends Error {
  status: number;
  type?: string;
  title?: string;
  detail?: string;
  /** Written to be actionable for both humans and LLMs — surface it. */
  suggestedFix?: string;

  constructor(status: number, payload: Record<string, unknown> | null, fallback: string) {
    const detail = (payload?.detail as string) || fallback;
    const fix = payload?.suggested_fix as string | undefined;
    super(fix ? `[${status}] ${detail} — ${fix}` : `[${status}] ${detail}`);
    this.status = status;
    this.type = payload?.type as string | undefined;
    this.title = payload?.title as string | undefined;
    this.detail = detail;
    this.suggestedFix = fix;
  }
}

export class NotFoundError extends D2BError {}
/** 409 — workbook busy / stale expected_version / taken name. Re-read,
 * re-apply, retry — deliberately not automatic. */
export class ConflictError extends D2BError {}
/** 403 — blocked by a column policy or missing scope. */
export class PolicyError extends D2BError {}

const errorFor = (status: number, payload: Record<string, unknown> | null, fallback: string) => {
  if (status === 404) return new NotFoundError(status, payload, fallback);
  if (status === 409) return new ConflictError(status, payload, fallback);
  if (status === 403) return new PolicyError(status, payload, fallback);
  return new D2BError(status, payload, fallback);
};

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

type Json = Record<string, unknown>;

const segment = (value: string) => encodeURIComponent(value);

export class D2BClient {
  readonly workbooks: Workbooks;
  readonly workspaces: Workspaces;
  readonly sources: Sources;
  readonly fileLinks: FileLinks;
  readonly tables: Tables;
  readonly query: Query;
  readonly transforms: Transforms;
  readonly versions: Versions;
  readonly jobs: Jobs;
  readonly sheets: Sheets;
  readonly charts: Charts;
  readonly exportApi: ExportApi;
  readonly webhooks: Webhooks;

  private readonly apiKey: string;
  private readonly baseUrl: string;
  private readonly maxRetries: number;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: D2BClientOptions) {
    this.apiKey = opts.apiKey;
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.maxRetries = opts.maxRetries ?? 3;
    this.fetchImpl = opts.fetch ?? fetch;
    this.workbooks = new Workbooks(this);
    this.workspaces = new Workspaces(this);
    this.sources = new Sources(this);
    this.fileLinks = new FileLinks(this);
    this.tables = new Tables(this);
    this.query = new Query(this);
    this.transforms = new Transforms(this);
    this.versions = new Versions(this);
    this.jobs = new Jobs(this);
    this.sheets = new Sheets(this);
    this.charts = new Charts(this);
    this.exportApi = new ExportApi(this);
    this.webhooks = new Webhooks(this);
  }

  async request<T = Json>(
    method: string,
    path: string,
    opts: {
      json?: Json;
      params?: Record<string, string | number | boolean | undefined>;
      form?: FormData;
      raw?: boolean;
      idempotencyKey?: string;
    } = {},
  ): Promise<T> {
    const url = new URL(`${this.baseUrl}/api/v1${path}`);
    for (const [k, v] of Object.entries(opts.params ?? {})) {
      if (v !== undefined) url.searchParams.set(k, String(v));
    }
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.apiKey}`,
      // Client attribution for the server's product analytics. Browsers
      // may strip or block custom headers on cross-origin calls; that is
      // fine — attribution is best-effort and the server falls back to
      // User-Agent classification.
      "X-D2B-Client": `d2b-node/${VERSION}`,
    };
    if (MUTATING.has(method)) {
      // Retry-safe by default: a network-level retry can never double-apply.
      headers["Idempotency-Key"] = opts.idempotencyKey ?? crypto.randomUUID();
    }
    let body: BodyInit | undefined;
    if (opts.form) {
      body = opts.form;
    } else if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.json);
    }

    let attempt = 0;
    let resp: Response;
    for (;;) {
      resp = await this.fetchImpl(url, { method, headers, body });
      if (RETRY_STATUSES.has(resp.status) && attempt < this.maxRetries) {
        attempt += 1;
        await sleep(Math.min(2 ** attempt * 250, 8000));
        continue;
      }
      break;
    }
    if (resp.status >= 400) {
      let payload: Record<string, unknown> | null = null;
      try {
        payload = (await resp.json()) as Record<string, unknown>;
      } catch {
        /* non-JSON error body */
      }
      throw errorFor(resp.status, payload, resp.statusText);
    }
    if (opts.raw) return (await resp.arrayBuffer()) as unknown as T;
    if (resp.status === 204) return undefined as unknown as T;
    const text = await resp.text();
    return (text ? JSON.parse(text) : undefined) as T;
  }
}

abstract class Resource {
  constructor(protected readonly c: D2BClient) {}
}

export interface ColumnSpec {
  name: string;
  type?: "VARCHAR" | "BIGINT" | "INTEGER" | "DOUBLE" | "DECIMAL" | "BOOLEAN" | "DATE" | "TIMESTAMP" | "TIME";
}

class Workbooks extends Resource {
  /** Create a workbook. `workspaceId` places it in that workspace
   * (creation-in-context — e.g. a provisioned customer workspace, or the
   * self-relative alias `"personal"`). A `workspace:<id>`-pinned key
   * defaults to its own workspace. */
  create(opts: { title?: string; workspaceId?: string } = {}): Promise<{ id: string }> {
    return this.c.request("POST", "/workbooks", {
      json: { title: opts.title, ...(opts.workspaceId ? { workspace_id: opts.workspaceId } : {}) },
    });
  }

  get(workbookId: string): Promise<Json> {
    return this.c.request("GET", `/workbooks/${segment(workbookId)}`);
  }

  async list(opts: { workspaceId?: string } = {}): Promise<Json[]> {
    const res = await this.c.request<{ workbooks: Json[] }>("GET", "/me/workbooks", {
      params: { workspace_id: opts.workspaceId },
    });
    return res.workbooks;
  }
}

class Workspaces extends Resource {
  /** Workspaces this credential can address, by name. `is_default` marks
   * where `workbooks.create` lands without `workspaceId`. */
  async list(): Promise<Json[]> {
    const res = await this.c.request<{ workspaces: Json[] }>("GET", "/me/workspaces");
    return res.workspaces;
  }
}

/** An xlsx on OneDrive / SharePoint under D2B version control: every save
 * becomes a version; diff, read a version's cells, revert D2B's side. D2B
 * never writes to the drive. */
class FileLinks extends Resource {
  track(workbookId: string, itemId: string, filename: string): Promise<Json> {
    return this.c.request("POST", `/workbooks/${segment(workbookId)}/file-links`, { json: { item_id: itemId, filename } });
  }
  async list(workbookId: string): Promise<Json[]> {
    const out = await this.c.request<{ links: Json[] }>("GET", `/workbooks/${segment(workbookId)}/file-links`);
    return out.links;
  }
  /** Track a file that lives on this machine: the bytes become version 1;
   * push later changes with `push`. */
  trackLocal(
    workbookId: string,
    file: Blob | Uint8Array,
    opts: { filename?: string; origin?: string; asName?: string; onExisting?: "auto" | "seed" | "replace" | "refuse" } = {},
  ): Promise<Json> {
    const form = new FormData();
    const blob = file instanceof Blob ? file : new Blob([file as BlobPart]);
    form.set("file", blob, opts.filename ?? "upload.xlsx");
    form.set("origin", opts.origin ?? "");
    form.set("on_existing", opts.onExisting ?? "auto");
    if (opts.asName) form.set("as_name", opts.asName);
    return this.c.request("POST", `/workbooks/${segment(workbookId)}/file-links/local`, { form });
  }
  /** Push the file's current bytes as the next version (identical bytes cut no version). */
  push(workbookId: string, linkId: string, file: Blob | Uint8Array, opts: { filename?: string; modifiedBy?: string } = {}): Promise<Json> {
    const form = new FormData();
    const blob = file instanceof Blob ? file : new Blob([file as BlobPart]);
    form.set("file", blob, opts.filename ?? "upload.xlsx");
    if (opts.modifiedBy) form.set("modified_by", opts.modifiedBy);
    return this.c.request("POST", `/workbooks/${segment(workbookId)}/file-links/${segment(linkId)}/versions`, { form });
  }
  sync(workbookId: string, linkId: string): Promise<Json> {
    return this.c.request("POST", `/workbooks/${segment(workbookId)}/file-links/${segment(linkId)}/sync`);
  }
  diff(workbookId: string, linkId: string, n: number, opts: { against?: number } = {}): Promise<Json> {
    const qs = opts.against != null ? `?against=${opts.against}` : "";
    return this.c.request("GET", `/workbooks/${segment(workbookId)}/file-links/${segment(linkId)}/versions/${n}/diff${qs}`);
  }
  cells(workbookId: string, linkId: string, n: number, opts: { sheet?: string } = {}): Promise<Json> {
    const qs = opts.sheet ? `?sheet=${encodeURIComponent(opts.sheet)}` : "";
    return this.c.request("GET", `/workbooks/${segment(workbookId)}/file-links/${segment(linkId)}/versions/${n}/cells${qs}`);
  }
  revert(workbookId: string, linkId: string, n: number): Promise<Json> {
    return this.c.request("POST", `/workbooks/${segment(workbookId)}/file-links/${segment(linkId)}/versions/${n}/revert`);
  }
}

class Sources extends Resource {
  /** Upload a file. `wait: true` uses the async form and polls the job
   * to completion — the friendly default for big files. */
  async upload(
    workbookId: string,
    file: Blob | Uint8Array,
    opts: { filename?: string; mode?: "auto" | "staged"; wait?: boolean; timeoutMs?: number } = {},
  ): Promise<Json> {
    const form = new FormData();
    const blob = file instanceof Blob ? file : new Blob([file as BlobPart]);
    form.set("file", blob, opts.filename ?? "upload.bin");
    form.set("mode", opts.mode ?? "auto");
    form.set("async", opts.wait ? "true" : "false");
    const out = await this.c.request<Json>("POST", `/workbooks/${segment(workbookId)}/sources`, { form });
    if (opts.wait) {
      const job = await this.c.jobs.wait(out.job_id as string, { timeoutMs: opts.timeoutMs });
      return job.result as Json;
    }
    return out;
  }

  async list(workbookId: string): Promise<Json[]> {
    const out = await this.c.request<{ sources: Json[] }>("GET", `/workbooks/${segment(workbookId)}/sources`);
    return out.sources;
  }
  /** Browse the linked Google Drive (`google_drive`) or OneDrive / SharePoint
   * (`sharepoint`) for importable files. Folders come first (`isFolder`);
   * SharePoint also searches by name (`query`). */
  async listCloud(
    provider: "sharepoint" | "google_drive",
    opts: { folderId?: string; query?: string } = {},
  ): Promise<Json[]> {
    const params = new URLSearchParams({ folder_id: opts.folderId ?? "root" });
    if (opts.query) params.set("q", opts.query);
    const out = await this.c.request<{ items: Json[] }>("GET", `/cloud-files/${segment(provider)}?${params}`);
    return out.items;
  }
  /** Import a drive file into the workbook (same ingest as upload). */
  importCloud(workbookId: string, provider: "sharepoint" | "google_drive", fileId: string, filename: string): Promise<Json> {
    return this.c.request("POST", `/workbooks/${segment(workbookId)}/sources/cloud`, {
      json: { provider, file_id: fileId, filename },
    });
  }

  analyze(workbookId: string, sourceName: string): Promise<Json> {
    return this.c.request("POST", `/workbooks/${segment(workbookId)}/sources/${segment(sourceName)}/analyze`);
  }

  materialize(workbookId: string, sourceName: string, body: Json = {}): Promise<Json> {
    return this.c.request("POST", `/workbooks/${segment(workbookId)}/sources/${segment(sourceName)}/materialize`, { json: body });
  }

  /** L0 reproduction: the uploaded bytes, byte-identical. */
  downloadOriginal(workbookId: string, sourceName: string): Promise<ArrayBuffer> {
    return this.c.request("GET", `/workbooks/${segment(workbookId)}/sources/${segment(sourceName)}/download`, { raw: true });
  }

  /** L1 reproduction: original xlsx, data regions refreshed (styles/charts kept).
   * `overrides` maps a region-linked table name → a replacement (e.g. transform)
   * table name, so a derived table renders into the template in place of its
   * source region — faithful output with the computation kept server-side. */
  renderTemplate(
    workbookId: string, sourceName: string,
    opts: { overrides?: Record<string, string> } = {},
  ): Promise<ArrayBuffer> {
    const params = opts.overrides ? { overrides: JSON.stringify(opts.overrides) } : undefined;
    return this.c.request("GET", `/workbooks/${segment(workbookId)}/sources/${segment(sourceName)}/render`, { raw: true, params });
  }

  /** Faithful edit in ONE call (G5): apply a SQL transform to a region and get
   * the original xlsx back with only that region's values changed. Folds
   * analyze + materialize + transform + L1 render. `transform.template` is a
   * `{{ artifact_name }}` SQL view with `{{ src }}` bound to the region table
   * (carry MIN("__d2b_row_id") to keep row positions on a folding merge).
   * `region` selects the target ({ region_id } or { sheet?, range }); omit for
   * the source's single data region. Styles/charts/in-region formulas kept;
   * the transform persists in the lineage DAG. */
  revise(
    workbookId: string,
    sourceName: string,
    body: {
      transform: { name: string; template: string; args?: Record<string, string>; artifact_name?: string };
      region?: { region_id?: string; sheet?: string; range?: string };
    },
  ): Promise<ArrayBuffer> {
    return this.c.request("POST", `/workbooks/${segment(workbookId)}/sources/${segment(sourceName)}/revise`, { raw: true, json: body });
  }
}

class Tables extends Resource {
  async list(workbookId: string): Promise<Json[]> {
    const out = await this.c.request<{ artifacts: Json[] }>("GET", `/workbooks/${segment(workbookId)}/tables`);
    return out.artifacts;
  }

  /** Create an empty editable table — upsert rows immediately after
   * with `expectedVersion: 1`. */
  async create(workbookId: string, name: string, columns: ColumnSpec[]): Promise<Json> {
    const out = await this.c.request<{ table: Json }>("POST", `/workbooks/${segment(workbookId)}/tables`, {
      json: { name, columns },
    });
    return out.table;
  }

  schema(workbookId: string, name: string): Promise<Json> {
    return this.c.request("GET", `/workbooks/${segment(workbookId)}/tables/${segment(name)}/schema`);
  }

  /** JSON Schema for one row of this table (policy-applied columns;
   * `__d2b_row_id` omitted = insert, set = update). Feed it to a
   * harness to constrained-decode `upsertRows` payloads. */
  async rowJsonSchema(workbookId: string, name: string): Promise<Json> {
    const out = await this.c.request<{ json_schema: Json }>(
      "GET",
      `/workbooks/${segment(workbookId)}/tables/${segment(name)}/schema`,
      { params: { format: "json-schema" } },
    );
    return out.json_schema;
  }

  rows(workbookId: string, name: string, opts: { limit?: number; offset?: number } = {}): Promise<Json> {
    return this.c.request("GET", `/workbooks/${segment(workbookId)}/tables/${segment(name)}/rows`, {
      params: { limit: opts.limit, offset: opts.offset },
    });
  }

  /** Optimistic-locked write; a stale version raises ConflictError —
   * re-read (`rows()` carries `edit_version`), re-apply, retry. */
  upsertRows(
    workbookId: string,
    name: string,
    rows: Json[],
    opts: { expectedVersion: number | null; actor?: string },
  ): Promise<Json> {
    return this.c.request("POST", `/workbooks/${segment(workbookId)}/tables/${segment(name)}/rows`, {
      json: { rows, expected_version: opts.expectedVersion, actor: opts.actor },
    });
  }

  /** A1 read facade: row 1 = header, data from row 2. */
  async a1(workbookId: string, name: string, range: string): Promise<unknown[][]> {
    const out = await this.c.request<{ values: unknown[][] }>(
      "GET",
      `/workbooks/${segment(workbookId)}/tables/${segment(name)}/a1`,
      { params: { range } },
    );
    return out.values;
  }

  /** A1 write facade: set a rectangle of cells. `values` is a row-major
   * block matching the range; grid row 2 is the first data row, rows past
   * the bottom append contiguously. Same optimistic locking as upsertRows. */
  writeA1(
    workbookId: string, name: string, range: string, values: unknown[][],
    opts: { expectedVersion: number | null; actor?: string },
  ): Promise<Json> {
    const body: Json = { range, values, expected_version: opts.expectedVersion };
    if (opts.actor) (body as Record<string, unknown>).actor = opts.actor;
    return this.c.request("PUT", `/workbooks/${segment(workbookId)}/tables/${segment(name)}/a1`, { json: body });
  }

  unarchive(workbookId: string, name: string): Promise<Json> {
    return this.c.request("POST", `/workbooks/${segment(workbookId)}/tables/${segment(name)}/unarchive`);
  }

  /** Rename a table/view's display name (id-identity refactor P4): the immutable
   * id and physical table are unchanged, so downstream transforms follow and
   * reads work under the new name. Returns the artifact with its (same) id. */
  rename(workbookId: string, name: string, newName: string): Promise<Json> {
    return this.c.request("PATCH", `/workbooks/${segment(workbookId)}/tables/${segment(name)}`, {
      json: { new_name: newName },
    });
  }

  setStyle(workbookId: string, name: string, spec: Json): Promise<Json> {
    return this.c.request("PUT", `/workbooks/${segment(workbookId)}/tables/${segment(name)}/style`, {
      json: { spec },
    });
  }

  /** The table's live-formula columns as `{ column: expr }`. */
  formulas(workbookId: string, name: string): Promise<Json> {
    return this.c.request("GET", `/workbooks/${segment(workbookId)}/tables/${segment(name)}/formulas`);
  }

  /** Deliver `column` as a per-row Excel formula instead of its frozen value
   * (e.g. `"{running} / {count}"`). Reference columns as `{column_name}`
   * placeholders, never A1 cell addresses; the stored value is untouched. The
   * response carries a `verification` report — whether the formula reproduces
   * the column's current values. */
  setFormula(
    workbookId: string, name: string, column: string, expr: string,
    opts: { actor?: string } = {},
  ): Promise<Json> {
    const body: Record<string, unknown> = { expr };
    if (opts.actor) body.actor = opts.actor;
    return this.c.request(
      "PUT",
      `/workbooks/${segment(workbookId)}/tables/${segment(name)}/columns/${segment(column)}/formula`,
      { json: body as Json },
    );
  }

  /** Drop a column's formula — it delivers its stored value again. */
  clearFormula(workbookId: string, name: string, column: string): Promise<Json> {
    return this.c.request(
      "DELETE",
      `/workbooks/${segment(workbookId)}/tables/${segment(name)}/columns/${segment(column)}/formula`,
    );
  }
}

class Query extends Resource {
  sql(workbookId: string, sql: string, opts: { limit?: number } = {}): Promise<Json> {
    return this.c.request("POST", `/workbooks/${segment(workbookId)}/query`, {
      json: { sql, limit: opts.limit ?? 1000 },
    });
  }
}

class Transforms extends Resource {
  /** Every authored transform currently producing an artifact — name, kind,
   * template and its output binding, one entry per output. The read half of
   * `create`: what `d2b pull` turns into `transforms/*.sql|py` files. */
  async list(workbookId: string): Promise<Json[]> {
    const out = await this.c.request<{ transforms: Json[] }>("GET", `/workbooks/${segment(workbookId)}/transforms`);
    return out.transforms;
  }

  /** Author a derived table — `{{ arg }}` placeholders bound via `args`
   * keep lineage traceable (hard-coded names are rejected). */
  create(
    workbookId: string,
    opts: { name: string; kind: "sql" | "python"; template: string; artifactName: string; args?: Record<string, string>; layer?: string },
  ): Promise<Json> {
    return this.c.request("POST", `/workbooks/${segment(workbookId)}/transforms`, {
      json: {
        name: opts.name,
        kind: opts.kind,
        template: opts.template,
        artifact_name: opts.artifactName,
        args: opts.args ?? {},
        layer: opts.layer,
      },
    });
  }
}

class Versions extends Resource {
  commit(workbookId: string, label: string, opts: { summary?: string } = {}): Promise<Json> {
    return this.c.request("POST", `/workbooks/${segment(workbookId)}/versions`, {
      json: { label, summary: opts.summary },
    });
  }

  async list(workbookId: string): Promise<Json[]> {
    const out = await this.c.request<{ versions: Json[] }>("GET", `/workbooks/${segment(workbookId)}/versions`);
    return out.versions;
  }

  revert(workbookId: string, label: string): Promise<Json> {
    return this.c.request("POST", `/workbooks/${segment(workbookId)}/versions/${segment(label)}/revert`);
  }
}

class Jobs extends Resource {
  get(jobId: string): Promise<Json> {
    return this.c.request("GET", `/jobs/${segment(jobId)}`);
  }

  /** Poll until terminal. Throws D2BError when the job failed. */
  async wait(jobId: string, opts: { timeoutMs?: number; intervalMs?: number } = {}): Promise<Json> {
    const deadline = Date.now() + (opts.timeoutMs ?? 600_000);
    for (;;) {
      const job = await this.get(jobId);
      if (job.status === "succeeded") return job;
      if (job.status === "failed") {
        throw new D2BError(500, { detail: (job.error as string) ?? "job failed" }, "job failed");
      }
      if (Date.now() > deadline) throw new Error(`job ${jobId} did not finish in time`);
      await sleep(opts.intervalMs ?? 1000);
    }
  }
}

export interface SheetBlock {
  kind: "heading" | "text" | "table_view" | "spacer";
  text?: string;
  table?: string;
  title?: string;
  rows?: number;
}

class Sheets extends Resource {
  /** Compose a presentation sheet: blocks REFERENCE tables (n:m); the
   * sheet owns no data. */
  put(workbookId: string, name: string, blocks: SheetBlock[]): Promise<Json> {
    return this.c.request("PUT", `/workbooks/${segment(workbookId)}/sheets/${segment(name)}`, {
      json: { spec: { blocks } },
    });
  }

  async list(workbookId: string): Promise<Json[]> {
    const out = await this.c.request<{ sheets: Json[] }>("GET", `/workbooks/${segment(workbookId)}/sheets`);
    return out.sheets;
  }

  /** One sheet: `{ name, spec: { blocks } }`. */
  get(workbookId: string, name: string): Promise<Json> {
    return this.c.request("GET", `/workbooks/${segment(workbookId)}/sheets/${segment(name)}`);
  }

  render(workbookId: string, name: string): Promise<ArrayBuffer> {
    return this.c.request("GET", `/workbooks/${segment(workbookId)}/sheets/${segment(name)}/render`, { raw: true });
  }

  delete(workbookId: string, name: string): Promise<void> {
    return this.c.request("DELETE", `/workbooks/${segment(workbookId)}/sheets/${segment(name)}`);
  }
}

class Charts extends Resource {
  /** Every chart with its rendered `config` and the `recipe` (tool + params)
   * it was generated from. Read-only — charts are re-generated from their
   * recipe, never edited as raw config; `config` is omitted on governed
   * workbooks. */
  async list(workbookId: string): Promise<Json[]> {
    const out = await this.c.request<{ charts: Json[] }>("GET", `/workbooks/${segment(workbookId)}/charts`);
    return out.charts;
  }
}

class ExportApi extends Resource {
  tables(
    workbookId: string,
    opts: { tables?: string[]; format?: "xlsx" | "csv"; formulaMode?: "values" | "preserve"; includeRowIds?: boolean; recordBranch?: boolean } = {},
  ): Promise<ArrayBuffer> {
    return this.c.request("POST", `/workbooks/${segment(workbookId)}/export`, {
      json: {
        tables: opts.tables ?? null,
        format: opts.format ?? "xlsx",
        formula_mode: opts.formulaMode ?? "values",
        include_row_ids: opts.includeRowIds ?? false,
        record_branch: opts.recordBranch ?? false,
      },
      raw: true,
    });
  }
}

class Webhooks extends Resource {
  create(url: string, opts: { events?: string[] } = {}): Promise<Json> {
    return this.c.request("POST", "/me/webhooks", { json: { url, events: opts.events } });
  }

  async list(): Promise<Json[]> {
    const out = await this.c.request<{ webhooks: Json[] }>("GET", "/me/webhooks");
    return out.webhooks;
  }

  delete(webhookId: string): Promise<void> {
    return this.c.request("DELETE", `/me/webhooks/${segment(webhookId)}`);
  }

  /** Verify `X-D2B-Signature` (= `sha256=<hex HMAC-SHA256>` of the RAW
   * request body) using WebCrypto. Carries no send time, so it cannot tell
   * a replay from the original — prefer {@link Webhooks.verifyDelivery}. */
  static async verifySignature(secret: string, payload: Uint8Array, signature: string): Promise<boolean> {
    return Webhooks.macMatches(secret, payload, signature);
  }

  /** Verify one delivery attempt: `X-D2B-Signature-V2` (= `sha256=<hex
   * HMAC-SHA256>` over `"{X-D2B-Delivery}.{X-D2B-Timestamp}." + raw body`)
   * and that `X-D2B-Timestamp` — the send time of this attempt, UNIX
   * seconds — is within `toleranceSeconds` (default 300) of now, so a
   * replayed request fails on its age even with a valid signature.
   * `headers` is the request's `Headers` or a plain record (names matched
   * case-insensitively); `X-D2B-Delivery` is the delivery's id, the same
   * on every retry — the key to process a delivery once. */
  static async verifyDelivery(
    secret: string,
    headers: Headers | Record<string, string | string[] | undefined>,
    payload: Uint8Array,
    opts: { toleranceSeconds?: number; now?: number } = {},
  ): Promise<boolean> {
    const deliveryId = Webhooks.header(headers, "x-d2b-delivery");
    const timestampRaw = Webhooks.header(headers, "x-d2b-timestamp");
    const signature = Webhooks.header(headers, "x-d2b-signature-v2");
    if (deliveryId === undefined || timestampRaw === undefined || signature === undefined) return false;
    if (!/^-?\d+$/.test(timestampRaw)) return false;
    const timestamp = Number(timestampRaw);
    const now = opts.now ?? Date.now() / 1000;
    if (Math.abs(now - timestamp) > (opts.toleranceSeconds ?? 300)) return false;
    const prefix = new TextEncoder().encode(`${deliveryId}.${timestamp}.`);
    const signed = new Uint8Array(prefix.length + payload.length);
    signed.set(prefix, 0);
    signed.set(payload, prefix.length);
    return Webhooks.macMatches(secret, signed, signature);
  }

  private static header(
    headers: Headers | Record<string, string | string[] | undefined>,
    name: string,
  ): string | undefined {
    if (typeof (headers as Headers).get === "function") {
      return (headers as Headers).get(name) ?? undefined;
    }
    for (const [key, value] of Object.entries(headers as Record<string, string | string[] | undefined>)) {
      if (key.toLowerCase() !== name) continue;
      if (Array.isArray(value)) return value[0];
      return value;
    }
    return undefined;
  }

  private static async macMatches(secret: string, data: Uint8Array, signature: string): Promise<boolean> {
    const key = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"],
    );
    const mac = new Uint8Array(await crypto.subtle.sign("HMAC", key, data as BufferSource));
    const hex = Array.from(mac).map((b) => b.toString(16).padStart(2, "0")).join("");
    const expected = `sha256=${hex}`;
    if (expected.length !== signature.length) return false;
    let diff = 0;
    for (let i = 0; i < expected.length; i++) diff |= expected.charCodeAt(i) ^ signature.charCodeAt(i);
    return diff === 0;
  }
}

export { Webhooks };
