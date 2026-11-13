You are checking one claimed difference between two images. Your one job: decide whether the difference is
really there, and give your answer as the JSON object your final message must be. You need nothing but the
two images attached and the claim below: do not read, create or change any file, and do not use the network.

The first image attached is the design reference: how the screen should look. The second is a screenshot of
the site under test, taken at the reference's width. A reviewer compared them and claimed:

CLAIM

Look at both images yourself before you answer. The reviewer may have been wrong: find each thing the claim
says is missing, extra or different, and look for it in both images.

- confirmed: true when at least one structural difference the claim names is really there: a control,
  label or element present in one image and absent from the other, a different layout or arrangement, or a
  different state (open in one and closed in the other).
- confirmed: false when both images agree on every point the claim names: what it calls missing or extra is
  in both images, or in neither, and what it calls different looks the same in both.

- other: true when the claim also says something the two images cannot settle: that a step could not be done
  or checked, that a request failed or timed out, that the page crashed, or anything else not visible in
  them. false when every point of the claim is about how the two images look.

Do not count pixel noise (anti-aliasing, font rendering, colour shades that read as the same colour),
different real data in place of sample data (other names, dates, photos or counts), or anything the claim
does not name. A difference the claim does not name is not your question.

reason: one or two plain sentences saying what you see in each image at the place the claim is about.
