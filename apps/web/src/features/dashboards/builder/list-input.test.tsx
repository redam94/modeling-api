import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { ListInput, parseList } from "./list-input";

function Harness({ seen }: { seen: string[][] }) {
  const [value, setValue] = useState<string[]>(["a"]);
  return (
    <ListInput
      aria-label="Choices"
      value={value}
      onChange={(v) => {
        seen.push(v);
        setValue(v);
      }}
    />
  );
}

describe("ListInput", () => {
  it("keeps the comma as it is typed, so a second item can follow", async () => {
    const seen: string[][] = [];
    render(<Harness seen={seen} />);
    const input = screen.getByLabelText<HTMLInputElement>("Choices");
    await userEvent.type(input, ", b,c");
    expect(input.value).toBe("a, b,c");
    expect(seen.at(-1)).toEqual(["a", "b", "c"]);
    await userEvent.tab();
    expect(input.value).toBe("a, b, c");
  });

  it("parses trimmed, non-empty items", () => {
    expect(parseList(" a, ,b ,", 5)).toEqual(["a", "b"]);
    expect(parseList("a,b,c", 2)).toEqual(["a", "b"]);
  });
});
