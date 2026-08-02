import requests, sys
sys.stdout.reconfigure(encoding="utf-8")

tests = [
    "The AI job apocalypse is not really a thing.",
    "Every agent needs a human.",
    "What do you want to do today?",
    "I kind of like it.",
    "Automation is a lie.",
]

for label, base in [("WITH BASE", True), ("NO BASE", False)]:
    r = requests.post("http://localhost:5173/api/phonetics",
                      json={"sentences": tests, "mode": "ai_ipa", "base": base})
    print(f"=== {label} ===")
    for i, item in enumerate(r.json()["results"]):
        nat = item["natural"]
        has_link = "‿" in nat
        print(f"[{i}] {tests[i]}")
        print(f"     {nat}")
        print(f"     {'✓ 有连读' if has_link else '✗ 无连读'}  source={item['source']}")
        print()
