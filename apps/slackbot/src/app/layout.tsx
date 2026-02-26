/** @jsxImportSource react */
import Link from "next/link";

export const metadata = {
  title: "Tempo AI",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap"
          rel="stylesheet"
        />
        <style
          dangerouslySetInnerHTML={{
            __html: `
              * { box-sizing: border-box; }

              @keyframes pulse-dot {
                0%, 100% { opacity: 1; }
                50% { opacity: 0.4; }
              }

              .state-dot-working {
                animation: pulse-dot 1.5s ease-in-out infinite;
              }

              .thread-card:hover {
                border-color: #3f3f46 !important;
                background-color: #18181b !important;
              }

              .nav-link:hover {
                color: #d4d4d8 !important;
              }

              .refresh-btn:hover {
                border-color: #52525b !important;
                color: #e4e4e7 !important;
              }

              .back-link:hover {
                color: #a1a1aa !important;
              }

              .tool-header:hover {
                background-color: #1c1c1e !important;
              }
            `,
          }}
        />
      </head>
      <body style={{ margin: 0, backgroundColor: "#09090b" }}>
        <nav
          style={{
            display: "flex",
            alignItems: "center",
            gap: "1.5rem",
            padding: "0.75rem 2rem",
            borderBottom: "1px solid #1c1c1e",
            backgroundColor: "#09090b",
            fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
          }}
        >
          <Link
            href="/"
            style={{
              color: "#fafafa",
              textDecoration: "none",
              fontWeight: 700,
              fontSize: "0.875rem",
              letterSpacing: "-0.01em",
            }}
          >
            Tempo AI
          </Link>
          <Link
            href="/threads"
            className="nav-link"
            style={{
              color: "#71717a",
              textDecoration: "none",
              fontSize: "0.8125rem",
              fontWeight: 500,
              transition: "color 0.15s",
            }}
          >
            Threads
          </Link>
        </nav>
        {children}
      </body>
    </html>
  );
}
