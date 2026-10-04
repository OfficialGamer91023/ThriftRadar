import type { Metadata } from "next";
import { Archivo, IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import Footer from "@/components/Footer";
import Nav from "@/components/Nav";
import "./globals.css";

// next/font downloads these at build time and serves them from web/out: the running app never calls Google.
const display = Archivo({ subsets: ["latin"], axes: ["wdth"], variable: "--font-display" });
const body = IBM_Plex_Sans({ subsets: ["latin"], weight: ["400", "500", "600"], variable: "--font-body" });
const mono = IBM_Plex_Mono({ subsets: ["latin"], weight: ["400", "500"], variable: "--font-mono" });

export const metadata: Metadata = {
  title: "ThriftRadar",
  description: "Shoes from your WhatsApp thrift group, searchable.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${display.variable} ${body.variable} ${mono.variable}`}>
      <body>
        <header className="topbar">
          <div className="wrap">
            <a className="wordmark" href="/">
              <span className="dot" aria-hidden="true" />
              ThriftRadar
            </a>
            <Nav />
          </div>
        </header>
        <main className="wrap">{children}</main>
        <Footer />
      </body>
    </html>
  );
}
