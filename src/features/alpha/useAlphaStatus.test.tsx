import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as alphaApi from "../../lib/api/alpha";
import { useAlphaStatus } from "./useAlphaStatus";

afterEach(() => vi.restoreAllMocks());

describe("useAlphaStatus", () => {
  it("reports loading synchronously when the gate check becomes enabled", async () => {
    let resolveStatus: ((value: alphaApi.AlphaStatus) => void) | undefined;
    vi.spyOn(alphaApi, "getAlphaStatus").mockReturnValue(
      new Promise((resolve) => {
        resolveStatus = resolve;
      }),
    );
    const { result, rerender } = renderHook(
      ({ enabled }) => useAlphaStatus(enabled),
      { initialProps: { enabled: false } },
    );

    expect(result.current.loading).toBe(false);
    rerender({ enabled: true });
    expect(result.current.loading).toBe(true);
    expect(result.current.status).toBeNull();

    await act(async () => {
      resolveStatus?.({
        gateEnabled: false,
        state: "disabled",
        locked: false,
        reason: null,
        graceUntil: null,
        deviceId: null,
        buildEol: false,
        buildEolRequired: false,
        updateUrl: null,
      });
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
  });
});
