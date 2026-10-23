"use client";

import { useEffect, useMemo, useState } from "react";

import { MarkdownContent } from "../components/markdown";
import { CopyButton, IconGraph } from "../components/ui";
import { LanguageToggle, useI18n } from "../i18n";
import { pageTitle, site, type Block, type Field, type Page, type Text } from "./site";

function href(slug: string): string {
  return slug === "introduction" ? "/docs/" : `/docs/${slug}/`;
}

export function DocsView({ slug }: { slug: string }) {
  const { lang, t } = useI18n();
  const [navOpen, setNavOpen] = useState(false);
  const page = site.pages[slug];
  const pick = (value: Text | null | undefined) => (value ? value[lang] || value.en : "");
  const flat = useMemo(() => site.nav.flatMap((group) => group.pages), []);
  const index = flat.findIndex((item) => item.slug === slug);
  const previous = index > 0 ? flat[index - 1] : null;
  const next = index >= 0 && index < flat.length - 1 ? flat[index + 1] : null;

  useEffect(() => {
    document.title = `${pageTitle(slug, lang)} · ${t("Veynod Docs")}`;
  }, [slug, lang, t]);

  useEffect(() => {
    if (!navOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setNavOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navOpen]);

  return (
    <div className="docs-shell">
      <a className="skip-link" href="#docs-content">{t("Skip to content")}</a>
      <header className="docs-header">
        <a className="docs-brand" href="/docs/">
          <span className="brand-mark"><IconGraph size={18} /></span>
          <span>Veynod <span className="docs-brand-sub">{t("Docs")}</span></span>
        </a>
        <button
          type="button"
          className="button ghost docs-nav-toggle"
          aria-expanded={navOpen}
          aria-controls="docs-nav"
          onClick={() => setNavOpen((open) => !open)}
        >
          {navOpen ? t("Close menu") : t("Menu")}
        </button>
        <nav className="docs-header-links" aria-label={t("Site")}>
          <a href="/dashboard/">{t("Console")}</a>
          <a href="/dashboard/login/">{t("Sign in")}</a>
          <LanguageToggle />
        </nav>
      </header>
      <div className="docs-body">
        <nav
          id="docs-nav"
          className={`docs-sidebar ${navOpen ? "open" : ""}`}
          aria-label={t("Documentation")}
        >
          {site.nav.map((group) => (
            <div className="docs-nav-group" key={group.id}>
              <div className="docs-nav-title">{pick(group.title)}</div>
              <ul>
                {group.pages.map((item) => (
                  <li key={item.slug}>
                    <a
                      href={href(item.slug)}
                      aria-current={item.slug === slug ? "page" : undefined}
                      className={item.slug === slug ? "active" : ""}
                    >
                      {pick(item.title)}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>
        <main id="docs-content" className="docs-main" tabIndex={-1}>
          {page ? (
            <article className="docs-article">
              <h1>{pageTitle(slug, lang)}</h1>
              <p className="docs-lead">{pick(page.summary)}</p>
              {page.blocks.map((block, position) => (
                <BlockView key={position} block={block} pick={pick} />
              ))}
              {page.api && <ApiReference groups={page.api} />}
              {page.errors && <ErrorReference errors={page.errors} />}
              <nav className="docs-pager" aria-label={t("Pages")}>
                {previous ? (
                  <a href={href(previous.slug)}>← {pick(previous.title)}</a>
                ) : <span />}
                {next ? <a href={href(next.slug)}>{pick(next.title)} →</a> : <span />}
              </nav>
            </article>
          ) : (
            <article className="docs-article">
              <h1>{t("Page not found")}</h1>
              <p><a href="/docs/">{t("Back to the docs home")}</a></p>
            </article>
          )}
        </main>
      </div>
    </div>
  );
}

function BlockView({ block, pick }: { block: Block; pick: (value: Text | null) => string }) {
  const { t } = useI18n();
  switch (block.type) {
    case "p":
      return <p>{pick(block.text)}</p>;
    case "h":
      return <h2>{pick(block.text)}</h2>;
    case "callout":
      return (
        <div className={`docs-callout ${block.tone === "warn" ? "warn" : ""}`} role="note">
          {pick(block.text)}
        </div>
      );
    case "link":
      return <p><a className="docs-link" href={block.href}>{pick(block.text)}</a></p>;
    case "list": {
      const items = block.items.map((item, position) => <li key={position}>{pick(item)}</li>);
      return block.ordered ? <ol>{items}</ol> : <ul>{items}</ul>;
    }
    case "table":
      return (
        <div className="table-wrap">
          <table className="data-table docs-table">
            <thead>
              <tr>{block.columns.map((column, position) => (
                <th key={position} scope="col">{pick(column)}</th>
              ))}</tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>{row.map((cell, cellIndex) => (
                  <td key={cellIndex}>{pick(cell)}</td>
                ))}</tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case "code":
      return (
        <figure className="docs-code">
          <figcaption>
            <span>{pick(block.label) || block.lang}</span>
            <CopyButton value={block.code} label={t("Copy")} />
          </figcaption>
          <pre tabIndex={0}><code>{block.code}</code></pre>
        </figure>
      );
    case "markdown":
      return <div className="docs-markdown"><MarkdownContent content={block.text} /></div>;
    default:
      return null;
  }
}

function ApiReference({ groups }: { groups: NonNullable<Page["api"]> }) {
  const { t } = useI18n();
  const [filter, setFilter] = useState("");
  const needle = filter.trim().toLowerCase();
  const visible = groups
    .map((group) => ({
      ...group,
      endpoints: group.endpoints.filter((endpoint) =>
        !needle || `${endpoint.method} ${endpoint.path} ${endpoint.summary}`.toLowerCase().includes(needle),
      ),
    }))
    .filter((group) => group.endpoints.length > 0);
  const total = visible.reduce((sum, group) => sum + group.endpoints.length, 0);
  return (
    <section className="docs-api">
      <div className="docs-callout" role="note">
        {t("Operators: the interactive Swagger UI is at /api-reference and needs the operator credential.")}
      </div>
      <label className="field docs-filter">
        <span>{t("Filter endpoints")}</span>
        <input
          type="search"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          placeholder="/v1/documents"
        />
      </label>
      <p className="docs-muted" aria-live="polite">{t("{count} endpoints", { count: total })}</p>
      {visible.map((group) => (
        <div key={group.group} className="docs-api-group">
          <h2 id={`api-${group.group}`}>{group.group}</h2>
          {group.endpoints.map((endpoint) => (
            <details key={`${endpoint.method} ${endpoint.path}`} className="docs-endpoint">
              <summary>
                <span className={`docs-method m-${endpoint.method.toLowerCase()}`}>{endpoint.method}</span>
                <code>{endpoint.path}</code>
                <span className="docs-endpoint-summary">{endpoint.summary}</span>
              </summary>
              {endpoint.description && <p>{endpoint.description}</p>}
              {endpoint.parameters.length > 0 && (
                <FieldTable title={t("Parameters")} fields={endpoint.parameters} showIn />
              )}
              {endpoint.body.length > 0 && <FieldTable title={t("Request body")} fields={endpoint.body} />}
              <p className="docs-muted">{t("Responses")}: {endpoint.responses.join(", ")}</p>
            </details>
          ))}
        </div>
      ))}
    </section>
  );
}

function FieldTable({
  title,
  fields,
  showIn = false,
}: {
  title: string;
  fields: Array<Field & { in?: string }>;
  showIn?: boolean;
}) {
  const { t } = useI18n();
  return (
    <div className="table-wrap">
      <table className="data-table docs-table">
        <caption>{title}</caption>
        <thead>
          <tr>
            <th scope="col">{t("Name")}</th>
            {showIn && <th scope="col">{t("In")}</th>}
            <th scope="col">{t("Type")}</th>
            <th scope="col">{t("Required")}</th>
            <th scope="col">{t("Description")}</th>
          </tr>
        </thead>
        <tbody>
          {fields.map((field) => (
            <tr key={`${field.in ?? ""}${field.name}`}>
              <td><code>{field.name}</code></td>
              {showIn && <td>{field.in}</td>}
              <td><code>{field.type}</code></td>
              <td>{field.required ? t("Yes") : t("No")}</td>
              <td>{field.description}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ErrorReference({ errors }: { errors: NonNullable<Page["errors"]> }) {
  const { t } = useI18n();
  const [filter, setFilter] = useState("");
  const needle = filter.trim().toLowerCase();
  const codes = errors.codes.filter((row) =>
    !needle || `${row.code} ${row.class} ${row.http}`.toLowerCase().includes(needle),
  );
  return (
    <section className="docs-errors">
      <h2>{t("Error classes")}</h2>
      <div className="table-wrap">
        <table className="data-table docs-table">
          <thead>
            <tr>
              <th scope="col">{t("Class")}</th>
              <th scope="col">{t("Action")}</th>
              <th scope="col">{t("Rule")}</th>
            </tr>
          </thead>
          <tbody>
            {errors.classes.map((row) => (
              <tr key={row.class}>
                <td><code>{row.class}</code></td>
                <td><code>{row.action}</code></td>
                <td>{row.rule}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h2>{t("Error codes")}</h2>
      <label className="field docs-filter">
        <span>{t("Filter codes")}</span>
        <input type="search" value={filter} onChange={(event) => setFilter(event.target.value)} />
      </label>
      <div className="table-wrap">
        <table className="data-table docs-table">
          <thead>
            <tr>
              <th scope="col">code</th>
              <th scope="col">HTTP</th>
              <th scope="col">{t("Class")}</th>
              <th scope="col">{t("What to do")}</th>
            </tr>
          </thead>
          <tbody>
            {codes.map((row) => (
              <tr key={row.code}>
                <td><code>{row.code}</code></td>
                <td>{row.http}</td>
                <td><code>{row.class}</code></td>
                <td>{row.rule}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h2>{t("Warnings")}</h2>
      <ul>
        {errors.warnings.map((row) => (
          <li key={row.code}><code>{row.code}</code> — {row.meaning}</li>
        ))}
      </ul>
      <h2>{t("Retry policy")}</h2>
      <pre tabIndex={0}><code>{JSON.stringify(errors.retry, null, 2)}</code></pre>
    </section>
  );
}
