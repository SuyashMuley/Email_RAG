from rag.ingest.chunker import chunk_text


def test_short_text_is_one_chunk():
    assert chunk_text("Hello there.") == ["Hello there."]


def test_empty_text_has_no_chunks():
    assert chunk_text("  \n\n ") == []


def test_chunks_respect_max_words_and_cover_all_text():
    paras = [f"Paragraph {i} " + "word " * 60 for i in range(10)]
    chunks = chunk_text("\n\n".join(paras), max_words=150, overlap_words=30)
    assert len(chunks) > 1
    assert all(len(c.split()) <= 150 for c in chunks)
    joined = "\n".join(chunks)
    assert all(f"Paragraph {i}" in joined for i in range(10))


def test_consecutive_chunks_overlap():
    paras = [f"P{i} " + "w " * 40 for i in range(8)]
    chunks = chunk_text("\n\n".join(paras), max_words=100, overlap_words=50)
    for a, b in zip(chunks, chunks[1:]):
        last_para = a.split("\n\n")[-1]
        assert last_para in b


def test_oversized_paragraph_is_split():
    text = " ".join(f"w{i}" for i in range(1000))
    chunks = chunk_text(text, max_words=200, overlap_words=0)
    assert len(chunks) == 5
    assert all(len(c.split()) <= 200 for c in chunks)
