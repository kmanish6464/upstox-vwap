def ensure_indexes(col) -> None:
    """Ensures indexes exist; cleans up duplicates if unique index creation fails."""
    try:
        # Attempt to create the unique index
        col.create_index([("SYMBOL", ASCENDING), ("DATE1", ASCENDING)], unique=True, name="sym_date")
    except pymongo.errors.DuplicateKeyError:
        log.warning("Duplicate data found. Cleaning up SYMBOL + DATE1 duplicates...")

        # Aggregation to find duplicates
        pipeline = [
            {"$group": {
                "_id": {"SYMBOL": "$SYMBOL", "DATE1": "$DATE1"},
                "ids": {"$push": "$_id"},
                "count": {"$sum": 1}
            }},
            {"$match": {"count": {"$gt": 1}}}
        ]

        duplicates = list(col.aggregate(pipeline))
        for doc in duplicates:
            # Keep the first document, delete the rest
            excess_ids = doc['ids'][1:]
            col.delete_many({"_id": {"$in": excess_ids}})

        # Retry creating the index after cleanup
        col.create_index([("SYMBOL", ASCENDING), ("DATE1", ASCENDING)], unique=True, name="sym_date")
        log.info("Duplicate cleanup successful and index created.")

    # Create supporting non-unique indexes
    col.create_index([("extraction_date", DESCENDING)], name="by_ext_date")
    col.create_index([("SERIES", ASCENDING)], name="by_series")