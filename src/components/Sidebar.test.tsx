// Locks the grouped-sidebar contract: groups render with their children,
// Settings is a standalone leaf rendered last, collapsing hides children,
// and the active item's group stays expanded.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Sidebar, type NavNode } from "./Sidebar";

const NODES: NavNode[] = [
  {
    kind: "group",
    label: "Workspace",
    children: [
      { key: "judge", label: "Judge", spec: "F001" },
      { key: "council", label: "Council", spec: "F031" },
    ],
  },
  {
    kind: "group",
    label: "System",
    children: [{ key: "ollama", label: "Ollama", spec: "F003" }],
  },
  { kind: "leaf", entry: { key: "settings", label: "Settings", spec: "F032" } },
];

afterEach(() => cleanup());

describe("Sidebar grouping", () => {
  it("renders group headers with their children expanded by default", () => {
    render(<Sidebar nodes={NODES} active="judge" onSelect={() => {}} />);
    expect(screen.getByRole("button", { name: "Collapse sidebar" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Workspace/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Judge/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Council/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Ollama/ })).toBeTruthy();
  });

  it("renders a collapsed rail with an expand button and no nav entries", () => {
    const onCollapsedChange = vi.fn();
    render(
      <Sidebar
        nodes={NODES}
        active="judge"
        onSelect={() => {}}
        collapsed
        onCollapsedChange={onCollapsedChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Expand sidebar" }));
    expect(onCollapsedChange).toHaveBeenCalledWith(false);
    expect(screen.queryByRole("button", { name: /Workspace/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Judge/ })).toBeNull();
  });

  it("renders Settings as the last item", () => {
    const { container } = render(
      <Sidebar nodes={NODES} active="judge" onSelect={() => {}} />,
    );
    const topItems = container.querySelectorAll(".sidebar-list > li");
    const last = topItems[topItems.length - 1];
    expect(within(last as HTMLElement).getByText("Settings")).toBeTruthy();
    // Settings is a leaf, not a group header.
    expect((last as HTMLElement).classList.contains("sidebar-group")).toBe(false);
  });

  it("collapsing a group hides its children", () => {
    render(<Sidebar nodes={NODES} active="ollama" onSelect={() => {}} />);
    // Collapse Workspace (active is in System, so Workspace can be collapsed).
    fireEvent.click(screen.getByRole("button", { name: /Workspace/ }));
    expect(screen.queryByRole("button", { name: /Judge/ })).toBeNull();
    // System (owns the active item) is still expanded.
    expect(screen.getByRole("button", { name: /Ollama/ })).toBeTruthy();
  });

  it("calls onSelect with the entry key when a child is clicked", () => {
    const onSelect = vi.fn();
    render(<Sidebar nodes={NODES} active="judge" onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("button", { name: /Council/ }));
    expect(onSelect).toHaveBeenCalledWith("council");
  });

  it("renders a disabled item greyed-out, non-clickable, with a hover reason", () => {
    const onSelect = vi.fn();
    const nodes: NavNode[] = [
      {
        kind: "group",
        label: "Workspace",
        children: [
          { key: "judge", label: "Judge", spec: "F001" },
          {
            key: "council",
            label: "Council",
            spec: "F031",
            disabled: true,
            disabledReason: "Connecting to the sidecar…",
          },
        ],
      },
    ];
    render(<Sidebar nodes={nodes} active="judge" onSelect={onSelect} />);
    const council = screen.getByRole("button", { name: /Council/ });
    expect(council).toBeDisabled();
    expect(council.getAttribute("title")).toMatch(/connecting to the sidecar/i);
    fireEvent.click(council);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("a manual collapse survives a parent re-render with a fresh nodes array", () => {
    // Regression: App re-renders every ~5s (health poll) and used to pass a
    // new `nodes` array identity, which re-ran the auto-expand effect and
    // re-opened a group the user had just collapsed.
    const { rerender } = render(
      <Sidebar nodes={NODES} active="ollama" onSelect={() => {}} />,
    );
    // Collapse Workspace (active item is in System, so this is allowed).
    fireEvent.click(screen.getByRole("button", { name: /Workspace/ }));
    expect(screen.queryByRole("button", { name: /Judge/ })).toBeNull();

    // Simulate the health-poll re-render: same content, brand-new array.
    const freshNodes: NavNode[] = NODES.map((n) =>
      n.kind === "group" ? { ...n, children: [...n.children] } : { ...n },
    );
    rerender(<Sidebar nodes={freshNodes} active="ollama" onSelect={() => {}} />);

    // Workspace must STAY collapsed.
    expect(screen.queryByRole("button", { name: /Judge/ })).toBeNull();
  });
});
