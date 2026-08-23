import type { Metadata } from "next";
import localFont from "next/font/local";
import "./globals.css";
import ThemeScript from "@/components/ThemeScript";
import ToastViewport from "@/components/common/ToastViewport";
import { AppShellProvider } from "@/context/AppShellContext";
import { TenantProvider } from "@/context/TenantContext";
import { I18nClientBridge } from "@/i18n/I18nClientBridge";

// Geist matches the public site (deeptutor.info) and stays crisp at the
// small UI sizes the composer/toolbars use, unlike the rounder Jakarta.
const fontSans = localFont({
  src: "./fonts/geist-latin.woff2",
  display: "swap",
  variable: "--font-sans",
  weight: "100 900",
});

const fontSerif = localFont({
  src: "./fonts/lora-latin.woff2",
  display: "swap",
  variable: "--font-serif",
  weight: "400 700",
});

export const metadata: Metadata = {
  title: "yFeiSTAI",
  description: "Agent-native intelligent learning companion",
  icons: {
    icon: [
      { url: "/favicon-16x16.png", sizes: "16x16", type: "image/png" },
      { url: "/favicon-32x32.png", sizes: "32x32", type: "image/png" },
    ],
    apple: "/apple-touch-icon.png",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="zh"
      suppressHydrationWarning
      data-scroll-behavior="smooth"
      className={`${fontSans.variable} ${fontSerif.variable}`}
    >
      <head>
        <ThemeScript />
      </head>
      <body
        className="font-sans bg-[var(--background)] text-[var(--foreground)]"
        suppressHydrationWarning
      >
        <AppShellProvider>
          <I18nClientBridge>
            <TenantProvider>{children}</TenantProvider>
          </I18nClientBridge>
          <ToastViewport />
        </AppShellProvider>
      </body>
    </html>
  );
}
