import type { ChangeEvent } from "react";

import "./CorpusPicker.css";
import DeleteCorpusButton from "./DeleteCorpusButton";
import type { CorpusSummary } from "../../lib/api/corpus";

type SharedProps = {
  corpora: CorpusSummary[];
  label?: string;
  loading?: boolean;
  disabled?: boolean;
  emptyLabel?: string;
  noCorporaLabel?: string;
  /**
   * F114 — when provided, each listed (selected) corpus gets a Delete affordance
   * that calls this after an explicit confirm. Omit to hide deletion entirely
   * (the default for read-only pickers such as the room editor).
   */
  onDeleteCorpus?: (name: string) => Promise<void> | void;
};

type SingleProps = SharedProps & {
  multiple?: false;
  value: string;
  onChange: (value: string) => void;
  allowEmpty?: boolean;
};

type MultipleProps = SharedProps & {
  multiple: true;
  value: string[];
  onChange: (value: string[]) => void;
  allowEmpty?: never;
};

export type CorpusPickerProps = SingleProps | MultipleProps;

function optionLabel(corpus: CorpusSummary): string {
  return `${corpus.name} (${countLabel(corpus)})`;
}

function countLabel(corpus: CorpusSummary): string {
  const source = corpus.source ?? "local";
  if (source === "unknown") return "missing from catalog";
  const unit = corpus.unit ?? (source === "remote" ? "chunks" : "files");
  if (unit === "chunks") {
    if (corpus.status === "indexing") {
      return `${corpus.readyCount}/${corpus.fileCount} chunks ready`;
    }
    return `${corpus.readyCount} chunks ready`;
  }
  return `${corpus.readyCount}/${corpus.fileCount} files ready`;
}

function missingCorpus(name: string): CorpusSummary {
  return {
    name,
    fileCount: 0,
    readyCount: 0,
    status: "missing",
    source: "unknown",
    unit: "files",
    capabilities: {
      list_files: false,
      upload_files: false,
      folder_watch: false,
      refresh_preview: false,
      remote_ingest: false,
    },
  };
}

function withSelectedCorpora(
  corpora: CorpusSummary[],
  selected: string[],
): CorpusSummary[] {
  const byName = new Map(corpora.map((c) => [c.name, c]));
  const out = corpora.slice();
  for (const name of selected) {
    if (name && !byName.has(name)) out.push(missingCorpus(name));
  }
  return out;
}

export default function CorpusPicker(props: CorpusPickerProps) {
  const {
    corpora,
    label = "Corpus",
    loading = false,
    disabled = false,
    noCorporaLabel = "No corpora available",
    onDeleteCorpus,
  } = props;
  const selected = props.multiple ? props.value : [props.value];
  const options = withSelectedCorpora(corpora, selected);
  const selectedSet = new Set(selected.filter(Boolean));
  const selectedCorpora = options.filter((c) => selectedSet.has(c.name));
  const selectDisabled = disabled || loading || options.length === 0;

  const onChange = (event: ChangeEvent<HTMLSelectElement>) => {
    if (props.multiple) {
      props.onChange(
        Array.from(event.currentTarget.selectedOptions).map((o) => o.value),
      );
    } else {
      props.onChange(event.currentTarget.value);
    }
  };

  // WS-F141-E: the multi-select case renders a styled checkbox list instead of a
  // bare native <select multiple> (which had no CSS and looked broken). The
  // single-select case keeps the normal dropdown so other call sites are
  // unchanged.
  const toggleMulti = (name: string, checked: boolean) => {
    if (!props.multiple) return;
    const next = checked
      ? [...selected.filter(Boolean), name]
      : selected.filter((n) => n && n !== name);
    props.onChange(Array.from(new Set(next)));
  };

  return (
    <div className="corpus-picker">
      {props.multiple ? (
        <div className="corpus-picker-field">
          <span className="corpus-picker-label-text">{label}</span>
          {options.length > 0 ? (
            <ul
              className="corpus-picker-choices"
              role="group"
              aria-label={label}
            >
              {options.map((corpus) => (
                <li key={corpus.name} className="corpus-picker-choice">
                  <label>
                    <input
                      type="checkbox"
                      checked={selectedSet.has(corpus.name)}
                      disabled={selectDisabled}
                      onChange={(e) => toggleMulti(corpus.name, e.currentTarget.checked)}
                    />{" "}
                    <span className="corpus-picker-choice-name">{corpus.name}</span>{" "}
                    <span className="corpus-picker-source-badge">{corpus.source ?? "local"}</span>{" "}
                    <span className="corpus-picker-choice-count">{countLabel(corpus)}</span>
                  </label>
                  {onDeleteCorpus && selectedSet.has(corpus.name) ? (
                    <DeleteCorpusButton
                      name={corpus.name}
                      onDelete={() => onDeleteCorpus(corpus.name)}
                    />
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : (
        <label className="corpus-picker-label">
          <span>{label}</span>
          <select
            aria-label={label}
            value={props.value}
            onChange={onChange}
            disabled={selectDisabled}
          >
            {props.allowEmpty ? (
              <option value="">{props.emptyLabel ?? "Select corpus"}</option>
            ) : null}
            {options.map((corpus) => (
              <option key={corpus.name} value={corpus.name}>
                {optionLabel(corpus)}
              </option>
            ))}
          </select>
        </label>
      )}
      {loading ? <p className="corpus-picker-note">Loading corpora…</p> : null}
      {!loading && options.length === 0 ? (
        <p className="corpus-picker-note">{noCorporaLabel}</p>
      ) : null}
      {!props.multiple && selectedCorpora.length > 0 ? (
        <ul className="corpus-picker-selection" aria-label={`${label} selection`}>
          {selectedCorpora.map((corpus) => (
            <li key={corpus.name}>
              <span>{corpus.name}</span>{" "}
              <span className="corpus-picker-source-badge">{corpus.source ?? "local"}</span>{" "}
              <span className="corpus-picker-status">
                {corpus.status ?? "ready"} · {countLabel(corpus)}
              </span>
              {onDeleteCorpus ? (
                <>
                  {" "}
                  <DeleteCorpusButton
                    name={corpus.name}
                    onDelete={() => onDeleteCorpus(corpus.name)}
                  />
                </>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
