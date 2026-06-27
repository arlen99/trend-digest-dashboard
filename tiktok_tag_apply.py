#!/usr/bin/env python3
"""Apply the hand-assessed visual-style / hook / trigger chips + notes to the TikTok
posts in dashboard/data.json (vision pass from the thumbnail contact sheet), so TikTok
cards carry the same analysis as Reels. videoText already set by tiktok_videotext.py;
this also blanks the few garbled OCR reads. Order matches the data.json TikTok order."""
import json
from pathlib import Path

DASH = Path(__file__).parent / "dashboard"

# index: (hookTypes, triggers, visualStyles, note, videoText_override|None)
TAGS = {
 0:([ "Storytime"],["Nostalgia"],["Bright/airy"],"Cultural procession doc — bright daylight, off the core niche.",""),
 1:(["POV","Tutorial/How-to"],["Relatability","Ego"],["Bright/airy"],"Behind-the-scenes creativity pep-talk over a shooting clip.",None),
 2:(["Tutorial/How-to"],["Ego"],["Moody/dark","Cinematic/teal-orange"],"Photo hack demo in moody overcast light.",None),
 3:([],["Awe"],["Cinematic/teal-orange"],"Golden-hour waterfall — pure cinematic B-roll.",None),
 4:(["Location reveal"],["Awe","Desire/Wanderlust"],["Drone/aerial","Cinematic/teal-orange"],"Epic Faroe cliffs + waterfall, drone reveal.",None),
 5:(["POV"],["Desire/Wanderlust"],["Bright/airy"],"Carefree leap in an alpine meadow, one-line caption.",None),
 6:(["Location reveal"],["Awe"],["Drone/aerial","Moody/dark"],"Dramatic Faroe cliff-lake aerial.",None),
 7:(["Tutorial/How-to"],["Ego"],["Bright/airy","Cinematic/teal-orange"],"Crowd-removal technique demo in a grand atrium.",None),
 8:(["POV","List/Number","Tutorial/How-to"],["Ego"],["Cinematic/teal-orange"],"Gear-driven creative-shot tutorial, neon grade.",""),
 9:(["POV"],["Desire/Wanderlust","Relatability"],["Cinematic/teal-orange"],"Couple-photographer POV in Paris.",None),
 10:(["Location reveal"],["Awe","Desire/Wanderlust"],["Drone/aerial","Bright/airy"],"Aerial of a dreamlike green islet.",None),
 11:(["Relatable confession"],["Ego","Relatability"],["Bright/airy"],"Gimbal behind-the-scenes over a city skyline.",None),
 12:(["Storytime"],["Awe","Nostalgia"],["Drone/aerial","Cinematic/teal-orange"],"Aerial down a sequoia road, reflective voiceover.",None),
 13:(["Bold claim"],["Desire/Wanderlust"],["Moody/dark","Cinematic/teal-orange"],"Camp under dramatic peaks, aspirational hook.",None),
 14:(["Location reveal"],["Awe","Desire/Wanderlust"],["Cinematic/teal-orange"],"Lone figure overlooking pine ridges, South Dakota.",None),
 15:(["Storytime"],["Relatability"],["Bright/airy"],"Candid cultural moment, warm and human.",None),
 16:(["Relatable confession"],["Desire/Wanderlust"],["Moody/dark","Cinematic/teal-orange"],"Couple at an Iceland waterfall, escapist caption.",None),
 17:([],["Ego"],["Moody/dark"],"Moody studio-lit portrait; off the travel core.",""),
 18:(["Location reveal"],["Desire/Wanderlust","Relatability"],["Cinematic/teal-orange","Bright/airy"],"Couple on a Dolomites hillside.",None),
 19:(["POV"],["Desire/Wanderlust"],["Moody/dark"],"Framed NYC skyline through a window, silhouettes.",None),
 20:(["Location reveal"],["Awe","Desire/Wanderlust"],["Drone/aerial","Cinematic/teal-orange"],"Raja Ampat aerial, teal lagoons.",None),
 21:(["POV"],["Desire/Wanderlust"],["Bright/airy"],"Carefree leap in an alpine meadow.",None),
 22:(["Bold claim"],["Desire/Wanderlust"],["Bright/airy"],"Two hikers on a Dolomites trail; 'your sign to go' hook.",None),
 23:([],["Nostalgia","Awe"],["Cinematic/teal-orange"],"Golden-hour meadow rest, one-word caption.",None),
}


def main():
    data = json.loads((DASH / "data.json").read_text())
    tt = [p for p in data["posts"] if p.get("platform") == "tiktok"]
    for i, p in enumerate(tt):
        if i not in TAGS:
            continue
        hk, tr, vs, note, vt = TAGS[i]
        p["hookTypes"], p["triggers"], p["visualStyles"], p["notes"] = hk, tr, vs, note
        if vt is not None:
            p["videoText"] = vt
    (DASH / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    chipped = sum(1 for p in tt if p.get("hookTypes") or p.get("triggers") or p.get("visualStyles"))
    print(f"Tagged {chipped}/{len(tt)} TikTok posts with chips + notes.")


if __name__ == "__main__":
    main()
