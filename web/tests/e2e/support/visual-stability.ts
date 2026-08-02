import { expect, type Page } from "@playwright/test";

const VISUAL_STABILITY_CSS = `
  *, *::before, *::after {
    animation-delay: 0s !important;
    animation-duration: 0s !important;
    animation-iteration-count: 1 !important;
    caret-color: transparent !important;
    scroll-behavior: auto !important;
    transition: none !important;
  }

  nextjs-portal {
    display: none !important;
  }
`;

export async function installVisualStability(page: Page): Promise<void> {
  await page.addStyleTag({ content: VISUAL_STABILITY_CSS });
  const hidesNextDevPortal = await page.evaluate(() => {
    const probe = document.createElement("nextjs-portal");
    document.documentElement.appendChild(probe);
    try {
      return getComputedStyle(probe).display === "none";
    } finally {
      probe.remove();
    }
  });
  expect(
    hidesNextDevPortal,
    "visual stability CSS must hide the Next.js development portal",
  ).toBe(true);
}
