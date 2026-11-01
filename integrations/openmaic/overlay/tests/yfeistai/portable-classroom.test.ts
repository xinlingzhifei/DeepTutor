import { describe, expect, test } from "vitest";

import { toPortableOpenMaicSceneContent } from "../../lib/yfeistai/content-generation";
import { assertPortableValue } from "../../lib/yfeistai/portable-classroom";

describe("portable rich text", () => {
  test("stores formula source without the upstream renderer HTML cache", () => {
    const element = {
      id: "formula-a",
      type: "latex",
      latex: "\\frac{3}{5} + \\sqrt{25}",
      html: '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"></path></svg>',
    };
    const result = toPortableOpenMaicSceneContent("slide", { elements: [element] });
    expect(result).toEqual({
      type: "slide",
      canvas: { elements: [{ id: element.id, type: element.type, latex: element.latex }] },
    });
    expect(() => assertPortableValue(result, "formula slide")).not.toThrow();
  });

  test.each([
    '<p style="font-size: 32px;"><strong>勾股定理：从公式到 3-4-5 例题</strong></p>',
    "<p style='color:#123456; font-weight:700; text-align:center'>a² + b² = c²</p>",
    '<span STYLE = "font-family: Arial; line-height: 1.5">Triangle</span>',
    '<p style="--accent: #123456; color: var(--accent)">Triangle</p>',
  ])("preserves inline formatting: %s", content => {
    const elements = [{ id: "text-a", type: "text", content }];
    expect(toPortableOpenMaicSceneContent("slide", { elements })).toEqual({
      type: "slide",
      canvas: { elements },
    });
  });

  test.each([
    "https://untrusted.example/image.png",
    "custom:private-resource",
    '<a href="custom:private-resource">link</a>',
    '<img src="/private/image.png">',
    '<p style="background-image: url(https://untrusted.example/image.png)">text</p>',
    '<p style="background-image: url(//untrusted.example/image.png)">text</p>',
    '<p style="background-image: url(/private/image.png)">text</p>',
    '<p style="background-image: url(data:image/png;base64,AAAA)">text</p>',
    '<p style="--source: custom:private-resource; color: red">text</p>',
    '<p style="color: red" onclick="javascript:alert(1)">text</p>',
    '<p style="color: red">file:///private/file</p>',
  ])("still rejects runtime locations: %s", content => {
    expect(() => assertPortableValue(content, "rich text")).toThrow(
      /non-portable location/,
    );
  });
});
