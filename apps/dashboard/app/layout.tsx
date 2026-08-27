import type { Metadata } from "next";
import "./globals.css";
import Nav from "@/components/Nav";
import VersionFooter from "@/components/VersionFooter";

export const metadata: Metadata = {
  title: "RecoveryOS Control Tower",
  description: "AI Revenue Recovery Control Plane",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Nav />
        {children}
        <VersionFooter />
      </body>
    </html>
  );
}
