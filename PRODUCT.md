# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

FastAPI backend with a vanilla HTML, CSS, and JavaScript interface. Windows runs the Relay service; phones can connect through a browser on the same local network. Zeroconf provides discovery between Windows services; HTTPS protects local transfers.

## Users

People transferring files and folders between Windows devices, or individual files between a Windows computer and a phone browser, on the same local network.

## Product Purpose

Move files directly between trusted devices without a cloud service. Windows peers can discover and pair with each other; a phone can connect by a one-time QR link to upload or download files through the Windows service.

## Positioning

Direct local transfer with both automatic discovery and explicit QR/manual pairing, using a short-lived secret and encrypted connection for each pairing.

## Operating Context

The Python service starts on each Windows device and opens a local browser interface. Windows peers meet on the same LAN. A phone browser connects to a Windows service through a short-lived QR link and can upload files to its receive folder or download files selected on the computer.

## Capabilities and Constraints

- Windows is the supported service runtime; Android and iPhone use a browser client and do not run the Python service.
- Transfer one or more files and complete folders.
- Support Zeroconf/mDNS discovery and QR or manual-code pairing.
- Use one-time, expiring pairing authorization and encrypted transport.
- Show device identity, pairing state, transfer progress, failures, and completion state.
- Handle large reliable transfers, retry or resume behavior, duplicate filenames, and folder boundaries safely.
- Do not require accounts, cloud infrastructure, or internet service.
- Support one-time phone invitations, authorized upload of individual files, and download of files staged on the Windows device.
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
