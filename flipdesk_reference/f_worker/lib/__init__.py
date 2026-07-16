# f_worker/lib — borrowed parts.
#
# Everything in here is lifted from the repo's battle-tested bot.py (the borrow list
# in the BUILD ORDER points at k_worker/*, which never existed in this repo; the real
# proven code lives in bot.py). We copy rather than re-implement, per "DO NOT
# re-implement any of these". lib/kalshi.py is the ONLY module that imports the
# cryptography stack, so the testable core never has to.
