from casita import craigslist, dedup


def test_source_priority_keeps_zillow_as_primary_source():
    assert dedup.SOURCE_PRIORITY["zillow"] == 0
    assert dedup.SOURCE_PRIORITY["zillow"] < dedup.SOURCE_PRIORITY["craigslist"]


def test_craigslist_location_regex_allows_flexible_whitespace():
    assert craigslist.SF_HOODS_RE.search("lake\tstreet")
    assert craigslist.MARIN_HOODS_RE.search("mill   valley")
