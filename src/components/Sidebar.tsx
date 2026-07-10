import { useEffect, useMemo, useState } from "react";
import type { FeatureKey } from "../App";
import errortaLogo from "../assets/errorta-logo.png";

export interface SidebarEntry {
  key: FeatureKey;
  label: string;
  spec: string;
  // When set, the item renders greyed-out and non-clickable; the reason is
  // shown on hover (e.g. Council before the sidecar advertises it).
  disabled?: boolean;
  disabledReason?: string;
}

// A nav node is either a collapsible group of entries or a single top-level
// ("leaf") entry. Settings is a leaf rendered last.
export type NavNode =
  | { kind: "group"; label: string; children: SidebarEntry[] }
  | { kind: "leaf"; entry: SidebarEntry };

interface Props {
  nodes: readonly NavNode[];
  active: FeatureKey;
  onSelect: (key: FeatureKey) => void;
  collapsed?: boolean;
  onCollapsedChange?: (collapsed: boolean) => void;
}

function EntryButton({
  entry,
  active,
  onSelect,
  nested,
}: {
  entry: SidebarEntry;
  active: FeatureKey;
  onSelect: (key: FeatureKey) => void;
  nested?: boolean;
}) {
  const isActive = entry.key === active;
  const disabled = Boolean(entry.disabled);
  return (
    <li>
      <button
        type="button"
        disabled={disabled}
        title={disabled ? entry.disabledReason : undefined}
        className={
          `sidebar-item${nested ? " sidebar-item-nested" : ""}` +
          (isActive ? " sidebar-item-active" : "") +
          (disabled ? " sidebar-item-disabled" : "")
        }
        onClick={() => {
          if (!disabled) onSelect(entry.key);
        }}
        aria-disabled={disabled || undefined}
        aria-current={isActive ? "page" : undefined}
      >
        <span className="sidebar-item-label">{entry.label}</span>
      </button>
    </li>
  );
}

export function Sidebar({
  nodes,
  active,
  onSelect,
  collapsed: sidebarCollapsed = false,
  onCollapsedChange,
}: Props) {
  // Groups default to expanded. Collapsing is per-group, tracked by label.
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  // The label of the group that owns the active item. Depend on this *string
  // value* (not the `nodes` array identity) so the auto-expand effect fires
  // only when the active item moves to a different group — NOT on every parent
  // re-render. App re-renders every ~5s from the health poll, which would
  // otherwise re-create `nodes` and re-expand a group the user just collapsed.
  const activeGroupLabel = useMemo(() => {
    const owning = nodes.find(
      (n) => n.kind === "group" && n.children.some((c) => c.key === active),
    );
    return owning && owning.kind === "group" ? owning.label : null;
  }, [nodes, active]);

  // When the user navigates to a tab, make sure its group is expanded so the
  // highlighted item is never hidden. Manual collapses of other groups (and of
  // the active group) are left untouched until the active group changes again.
  useEffect(() => {
    if (!activeGroupLabel) return;
    setCollapsed((cur) => {
      if (!cur.has(activeGroupLabel)) return cur;
      const next = new Set(cur);
      next.delete(activeGroupLabel);
      return next;
    });
  }, [activeGroupLabel]);

  const toggle = (label: string) =>
    setCollapsed((cur) => {
      const next = new Set(cur);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });

  return (
    <nav
      className={"sidebar" + (sidebarCollapsed ? " sidebar-collapsed" : "")}
      aria-label="Feature navigation"
    >
      <div className="sidebar-brand">
        {!sidebarCollapsed ? (
          <>
            <img
              src={errortaLogo}
              alt="Errorta"
              className="sidebar-brand-logo"
            />
            <div className="sidebar-brand-text">
              <span className="sidebar-brand-name">Errorta</span>
              <span className="sidebar-brand-version">v0.1.0-alpha.0</span>
            </div>
          </>
        ) : null}
        <button
          type="button"
          className="sidebar-collapse-toggle"
          aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-expanded={!sidebarCollapsed}
          aria-controls={sidebarCollapsed ? undefined : "sidebar-navigation"}
          title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          onClick={() => onCollapsedChange?.(!sidebarCollapsed)}
        >
          <span
            className={
              "sidebar-collapse-icon" +
              (sidebarCollapsed ? " sidebar-collapse-icon-collapsed" : "")
            }
            aria-hidden="true"
          />
        </button>
      </div>
      {!sidebarCollapsed ? (
        <ul className="sidebar-list" id="sidebar-navigation">
          {nodes.map((node) => {
            if (node.kind === "leaf") {
              return (
                <EntryButton
                  key={node.entry.key}
                  entry={node.entry}
                  active={active}
                  onSelect={onSelect}
                />
              );
            }
            const isCollapsed = collapsed.has(node.label);
            const groupId = `sidebar-group-${node.label.toLowerCase()}`;
            const hasActive = node.children.some((c) => c.key === active);
            return (
              <li key={node.label} className="sidebar-group">
                <button
                  type="button"
                  className={`sidebar-group-header${hasActive ? " sidebar-group-has-active" : ""}`}
                  onClick={() => toggle(node.label)}
                  aria-expanded={!isCollapsed}
                  aria-controls={groupId}
                >
                  <span className="sidebar-group-caret" aria-hidden="true">
                    {isCollapsed ? "▸" : "▾"}
                  </span>
                  <span className="sidebar-group-label">{node.label}</span>
                </button>
                {!isCollapsed && (
                  <ul className="sidebar-sublist" id={groupId}>
                    {node.children.map((entry) => (
                      <EntryButton
                        key={entry.key}
                        entry={entry}
                        active={active}
                        onSelect={onSelect}
                        nested
                      />
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      ) : null}
    </nav>
  );
}
