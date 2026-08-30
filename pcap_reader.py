"""
pcap_reader.py  —  turn a binary capture file into readable text

A .pcap / .pcapng file is BINARY -- the language model (and the vision model)
cannot read it directly. Wireshark itself is just a viewer that parses those
bytes into a human-readable packet list. This module does the same job in
code: it parses the capture with scapy (a pure-Python parser -- no Wireshark
or tshark install required) and produces a compact, Wireshark-style text
summary that the tutor model CAN read and reason about.

What the summary looks like (one line per packet, like Wireshark's list pane):
    No.  Time     Source            Destination       Proto  Length  Info
    1    0.000000 192.168.1.10      93.184.216.34     TCP    74      49152 -> 80 [SYN] Seq=1000
    2    0.050000 93.184.216.34     192.168.1.10      TCP    74      80 -> 49152 [SYN, ACK] ...

Design choices that matter for the homework guard:
  - This produces a faithful, factual summary of the capture -- it does NOT
    interpret or answer anything. The Socratic guard in ask.py still decides
    whether to guide vs. reveal. The summary is just "what the trace contains,"
    the same information the student sees in their own Wireshark window.
  - Large captures are truncated (MAX_PACKETS) so a huge file can't blow up the
    prompt; the summary says how many packets were omitted.
"""

from io import BytesIO

MAX_PACKETS = 120          # cap how many packets we summarize into the prompt
MAX_PAYLOAD_PREVIEW = 80   # chars of ASCII payload preview per packet


def summarize_pcap_bytes(data):
    """Parse raw pcap/pcapng bytes -> (summary_text, ok). ok is False if the
    file couldn't be parsed (corrupt, not actually a capture, scapy missing)."""
    try:
        from scapy.all import rdpcap
    except ImportError:
        return ("(Could not read the capture file: the 'scapy' library isn't installed. "
                "Run: pip install scapy)", False)
    try:
        packets = rdpcap(BytesIO(data))
    except Exception as error:
        return (f"(Could not parse this as a pcap/pcapng capture: {error})", False)

    total = len(packets)
    if total == 0:
        return ("(The capture file is empty -- no packets found.)", False)

    lines = [f"WIRESHARK CAPTURE SUMMARY -- {total} packet(s)"
             + (f", showing the first {MAX_PACKETS}" if total > MAX_PACKETS else ""),
             "No.  Time      Source             Destination        Proto Len   Info"]

    t0 = float(packets[0].time)
    for index, pkt in enumerate(packets[:MAX_PACKETS], start=1):
        lines.append(_summarize_packet(index, pkt, t0))

    if total > MAX_PACKETS:
        lines.append(f"... ({total - MAX_PACKETS} more packets not shown)")

    # A short protocol tally helps the model (and the student) see the shape
    # of the capture at a glance.
    tally = _protocol_tally(packets[:MAX_PACKETS])
    if tally:
        lines.append("")
        lines.append("Protocol counts: " + ", ".join(f"{k}={v}" for k, v in tally.items()))

    return ("\n".join(lines), True)


def _summarize_packet(index, pkt, t0):
    from scapy.all import IP, IPv6, TCP, UDP, ICMP, ARP, Raw, DNS

    rel_time = float(pkt.time) - t0
    length = len(pkt)
    src = dst = "?"
    proto = "?"
    info = ""

    if pkt.haslayer(ARP):
        arp = pkt[ARP]
        proto = "ARP"
        src, dst = arp.psrc, arp.pdst
        info = f"who-has {arp.pdst} tell {arp.psrc}" if arp.op == 1 else f"{arp.psrc} is-at {arp.hwsrc}"
    elif pkt.haslayer(IP) or pkt.haslayer(IPv6):
        ip = pkt[IP] if pkt.haslayer(IP) else pkt[IPv6]
        src, dst = ip.src, ip.dst
        if pkt.haslayer(TCP):
            tcp = pkt[TCP]
            proto = "TCP"
            flag_names = _tcp_flags(tcp.flags)
            info = f"{tcp.sport} -> {tcp.dport} [{flag_names}] Seq={tcp.seq} Ack={tcp.ack} Win={tcp.window}"
            if pkt.haslayer(Raw):
                info += _payload_preview(pkt[Raw].load)
        elif pkt.haslayer(UDP):
            udp = pkt[UDP]
            proto = "UDP"
            info = f"{udp.sport} -> {udp.dport} Len={udp.len}"
            if pkt.haslayer(DNS):
                proto = "DNS"
                info += _dns_info(pkt[DNS])
        elif pkt.haslayer(ICMP):
            proto = "ICMP"
            info = f"type={pkt[ICMP].type} code={pkt[ICMP].code}"
        else:
            proto = ip.get_field("proto").i2s.get(ip.proto, str(ip.proto)) if hasattr(ip, "proto") else "IP"
    else:
        proto = pkt.lastlayer().name if hasattr(pkt, "lastlayer") else "?"

    return (f"{index:<4} {rel_time:<9.6f} {src:<18} {dst:<18} {proto:<5} {length:<5} {info}")


def _tcp_flags(flags):
    names = []
    mapping = [("S", "SYN"), ("A", "ACK"), ("F", "FIN"), ("R", "RST"),
               ("P", "PSH"), ("U", "URG"), ("E", "ECE"), ("C", "CWR")]
    flag_str = str(flags)
    for short, long in mapping:
        if short in flag_str:
            names.append(long)
    return ", ".join(names) if names else "none"


def _payload_preview(load):
    try:
        text = load.decode("ascii", errors="replace")
    except Exception:
        return ""
    text = "".join(ch if 32 <= ord(ch) < 127 else "." for ch in text)
    text = text.strip()
    if not text:
        return ""
    if len(text) > MAX_PAYLOAD_PREVIEW:
        text = text[:MAX_PAYLOAD_PREVIEW] + "..."
    return f'  payload="{text}"'


def _dns_info(dns):
    try:
        if dns.qr == 0 and dns.qd is not None:
            return f" query {dns.qd.qname.decode(errors='replace')}"
        if dns.qr == 1:
            return f" response ({dns.ancount} answer(s))"
    except Exception:
        pass
    return ""


def _protocol_tally(packets):
    from scapy.all import TCP, UDP, ICMP, ARP, DNS
    counts = {}
    for pkt in packets:
        if pkt.haslayer(DNS):
            key = "DNS"
        elif pkt.haslayer(TCP):
            key = "TCP"
        elif pkt.haslayer(UDP):
            key = "UDP"
        elif pkt.haslayer(ICMP):
            key = "ICMP"
        elif pkt.haslayer(ARP):
            key = "ARP"
        else:
            key = "Other"
        counts[key] = counts.get(key, 0) + 1
    return counts
