# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

FastAPI backend with a vanilla HTML, CSS, and JavaScript interface. Windows is the first supported runtime. Zeroconf provides local discovery; browser-compatible cryptography provides encrypted transport.

## Users

General consumers transferring files and folders between Windows devices on the same local network, primarily when cloud upload, accounts, or internet access are inconvenient or unavailable.

## Product Purpose

Move files directly between trusted devices without a cloud service. Success means a consumer can discover or pair a nearby device, choose files or folders, see clear progress, and receive a collision-safe result.

## Positioning

Direct local transfer with both automatic discovery and explicit QR/manual pairing, using a short-lived secret and encrypted connection for each pairing.

## Operating Context

The Python service starts on each Windows device and opens a local browser interface. Devices meet on the same LAN. One device selects and sends files or folders; the peer receives them into a configurable destination.

## Capabilities and Constraints

- Windows is the only supported platform in the first release.
- Transfer one or more files and complete folders.
- Support Zeroconf/mDNS discovery and QR or manual-code pairing.
- Use one-time, expiring pairing authorization and encrypted transport.
- Show device identity, pairing state, transfer progress, failures, and completion state.
- Handle large reliable transfers, retry or resume behavior, duplicate filenames, and folder boundaries safely.
- Do not require accounts, cloud infrastructure, or internet service.
- Open-source licensing, installer packaging, background service operation, and public-release distribution remain undecided.

## Brand Commitments

The product is named **Relay**. Use direct, plain-language copy that explains pairing, transfer direction, and safety without technical jargon. No logo, illustration system, or existing brand assets have been supplied.

## Evidence on Hand

No product assets, customer evidence, performance benchmarks, testimonials, or brand commitments were supplied.

## Product Principles

- Local means direct, private, and understandable to the user.
- Pairing should be explicit and visibly temporary.
- Transfer state must remain clear from first interaction through completion.
- Reliability matters more than hidden cleverness or feature breadth.
- Never trade path safety or data integrity for convenience.

## Accessibility & Inclusion

Use semantic controls, visible keyboard focus, sufficient contrast, plain-language status text, and progress that does not rely on color alone.
