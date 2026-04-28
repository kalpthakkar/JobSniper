#!/usr/bin/env python
"""Debug HTML entity unescaping."""
from core.description_parser import parse_html_description

realistic_html = '''<p>As a leading financial services and healthcare technology company based on revenue, SS&amp;C is headquartered in Windsor, Connecticut, and has 27,000&#43; employees in 35 countries.</p><p></p><p><b><u>Job Description</u></b></p><p></p><div><span><b><u><span>Get To Know The Team:</span></u></b></span></div><div><div><p><span>SS&amp;C Eze is seeking a Senior Software Engineer to join the Eze OMS team based out of our Boston headquarters.</span></p></div></div><p><b>Responsibilities:</b></p><ul><li>Design and implement high-performance systems</li><li>Lead code reviews and mentor junior developers</li><li>Collaborate with product and design teams</li><li>Participate in architecture decisions</li></ul><p><b>Required Skills:</b></p><ul><li>10&#43; years of software development experience</li><li>Strong knowledge of Java or C&#43;&#43;</li><li>Experience with distributed systems</li><li>Excellent communication skills</li></ul><p>Benefits:</p><ul><li>Competitive salary</li><li>Health insurance</li><li>401(k) matching</li><li>Remote work options</li></ul>'''

description = parse_html_description(realistic_html)

# Find all ampersands
for i, char in enumerate(description):
    if char == "&":
        context = description[max(0, i-20):i+20]
        print(f"Found '&' at position {i}: ...{context}...")

print(f"\nTotal description length: {len(description)}")
print(f"Ampersand count: {description.count('&')}")

if "&" in description:
    print("\n⚠️ Some ampersands remain - this might be legitimate (e.g., in 'A & B')")
    print("Showing context:")
    parts = description.split("&")
    for i, part in enumerate(parts[:-1]):
        next_part = parts[i+1]
        print(f"  '{part[-10:]}' & '{next_part[:10]}'")
