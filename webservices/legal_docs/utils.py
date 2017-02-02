import logging
import re
import pytest
from collections import defaultdict
from urllib.parse import urlencode

from webservices.env import env
from webservices.legal_docs import DOCS_INDEX
from webservices.utils import create_eregs_link
from webservices.rest import db

from .reclassify_statutory_citation import reclassify_current_mur_statutory_citation

logger = logging.getLogger(__name__)

""

SUBJECTS = """
    SELECT subject.description AS subj, relatedsubject.description AS rel
    FROM fecmur.case_subject
    JOIN fecmur.subject USING (subject_id)
    LEFT OUTER JOIN fecmur.relatedsubject USING (subject_id, relatedsubject_id)
    WHERE case_id = %s
"""

ELECTION_CYCLES = """
    SELECT election_cycle::INT
    FROM fecmur.electioncycle
    WHERE case_id = %s
"""

PARTICIPANTS = """
    SELECT entity_id, name, role.description AS role
    FROM fecmur.players
    JOIN fecmur.role USING (role_id)
    JOIN fecmur.entity USING (entity_id)
    WHERE case_id = %s
"""

DOCUMENTS = """
    SELECT document_id, category, description, ocrtext,
        fileimage, length(fileimage) AS length,
        doc_order_id, document_date
    FROM fecmur.document
    WHERE case_id = %s
    ORDER BY doc_order_id, document_date desc, document_id DESC;
"""
# TODO: Check if document order matters

VIOLATIONS = """
    SELECT entity_id, stage, statutory_citation, regulatory_citation
    FROM fecmur.violations
    WHERE case_id = %s
    ;
"""

OPEN_AND_CLOSE_DATES = """
    SELECT min(event_date), max(event_date)
    FROM fecmur.calendar
    WHERE case_id = %s;
"""

DISPOSITION_DATA = """
    SELECT fecmur.event.event_name,
        fecmur.settlement.final_amount, fecmur.entity.name, violations.statutory_citation,
        violations.regulatory_citation
    FROM fecmur.calendar
    INNER JOIN fecmur.event ON fecmur.calendar.event_id = fecmur.event.event_id
    INNER JOIN fecmur.entity ON fecmur.entity.entity_id = fecmur.calendar.entity_id
    LEFT JOIN (SELECT * FROM fecmur.relatedobjects WHERE relation_id = 1) AS relatedobjects
        ON relatedobjects.detail_key = fecmur.calendar.entity_id
    LEFT JOIN fecmur.settlement ON fecmur.settlement.settlement_id = relatedobjects.master_key
    LEFT JOIN (SELECT * FROM fecmur.violations WHERE stage = 'Closed' AND case_id = {0}) AS violations
        ON violations.entity_id = fecmur.calendar.entity_id
    WHERE fecmur.calendar.case_id = {0}
        AND event_name NOT IN ('Complaint/Referral', 'Disposition')
    ORDER BY fecmur.event.event_name ASC, fecmur.settlement.final_amount DESC NULLS LAST, event_date DESC;
"""

COMMISSION_VOTES = """
SELECT vote_date, action from fecmur.commission
WHERE case_id = %s
ORDER BY vote_date desc;
"""

STATUTE_REGEX = re.compile(r'(?<!\(|\d)(?P<section>\d+([a-z](-1)?)?)')
REGULATION_REGEX = re.compile(r'(?<!\()(?P<part>\d+)(\.(?P<section>\d+))?')

def get_election_cycles(case_id):
    election_cycles = []
    with db.engine.connect() as conn:
        rs = conn.execute(ELECTION_CYCLES, case_id)
        for row in rs:
            election_cycles.append(row['election_cycle'])
    return election_cycles

def get_open_and_close_dates(case_id):
    with db.engine.connect() as conn:
        rs = conn.execute(OPEN_AND_CLOSE_DATES, case_id)
        open_date, close_date = rs.fetchone()
    return open_date, close_date

def get_disposition(case_id):
    with db.engine.connect() as conn:
        rs = conn.execute(DISPOSITION_DATA.format(case_id))
        disposition_data = []
        for row in rs:
            citations = parse_statutory_citations(row['statutory_citation'], case_id, row['name'])
            citations.extend(parse_regulatory_citations(row['regulatory_citation'], case_id, row['name']))
            disposition_data.append({'disposition': row['event_name'], 'penalty': row['final_amount'],
                'respondent': row['name'], 'citations': citations})

        rs = conn.execute(COMMISSION_VOTES, case_id)
        disposition_text = []
        for row in rs:
            disposition_text.append({'vote_date': row['vote_date'], 'text': row['action']})
        return {'text': disposition_text, 'data': disposition_data}

def get_commission_votes(case_id):
    with db.engine.connect() as conn:
        rs = conn.execute(COMMISSION_VOTES, case_id)
        commission_votes = []
        for row in rs:
            commission_votes.append({'vote_date': row['vote_date'], 'action': row['action']})
        return commission_votes

def get_participants(case_id):
    participants = {}
    with db.engine.connect() as conn:
        rs = conn.execute(PARTICIPANTS, case_id)
        for row in rs:
            participants[row['entity_id']] = {
                'name': row['name'],
                'role': row['role'],
                'citations': defaultdict(list)
            }
    return participants

def get_sorted_respondents(participants):
    """
    Returns the respondents in a MUR sorted in the order of most important to least important
    """
    SORTED_RESPONDENT_ROLES = ['Primary Respondent', 'Respondent', 'Previous Respondent']
    respondents = []
    for role in SORTED_RESPONDENT_ROLES:
        respondents.extend(sorted([p['name'] for p in participants if p['role'] == role]))
    return respondents

def get_subjects(case_id):
    subjects = []
    with db.engine.connect() as conn:
        rs = conn.execute(SUBJECTS, case_id)
        for row in rs:
            if row['rel']:
                subject_str = row['subj'] + "-" + row['rel']
            else:
                subject_str = row['subj']
            subjects.append(subject_str)
    return subjects

def assign_citations(participants, case_id):
    with db.engine.connect() as conn:
        rs = conn.execute(VIOLATIONS, case_id)
        for row in rs:
            entity_id = row['entity_id']
            if entity_id not in participants:
                logger.warn("Entity %s from violations not found in participants for case %s", entity_id, case_id)
                continue
            participants[entity_id]['citations'][row['stage']].extend(
                parse_statutory_citations(row['statutory_citation'], case_id, entity_id))
            participants[entity_id]['citations'][row['stage']].extend(
                parse_regulatory_citations(row['regulatory_citation'], case_id, entity_id))

def parse_statutory_citations(statutory_citation, case_id, entity_id):
    citations = []
    if statutory_citation:
        statutory_citation = remove_reclassification_notes(statutory_citation)
        matches = list(STATUTE_REGEX.finditer(statutory_citation))
        for index, match in enumerate(matches):
            section = match.group('section')
            orig_title, new_title, new_section = reclassify_current_mur_statutory_citation(section)
            url = 'https://api.fdsys.gov/link?' +\
                urlencode([
                    ('collection', 'uscode'),
                    ('year', 'mostrecent'),
                    ('link-type', 'html'),
                    ('title', new_title),
                    ('section', new_section)
                ])
            if index == len(matches) - 1:
                match_text = statutory_citation[match.start():]
            else:
                match_text = statutory_citation[match.start():matches[index + 1].start()]
            text = match_text.rstrip(' ,;')
            citations.append({'text': text, 'type': 'statute', 'title': orig_title, 'url': url})
        if not citations:
            logger.warn("Cannot parse statutory citation %s for Entity %s in case %s",
                    statutory_citation, entity_id, case_id)
    return citations

def parse_regulatory_citations(regulatory_citation, case_id, entity_id):
    citations = []
    if regulatory_citation:
        matches = list(REGULATION_REGEX.finditer(regulatory_citation))
        for index, match in enumerate(matches):
            part = match.group('part')
            section = match.group('section')
            url = create_eregs_link(part, section)
            if index == len(matches) - 1:
                match_text = regulatory_citation[match.start():]
            else:
                match_text = regulatory_citation[match.start():matches[index + 1].start()]
            text = match_text.rstrip(' ,;')
            citations.append({'text': text, 'type': 'regulation', 'title': '11', 'url': url})
        if not citations:
            logger.warn("Cannot parse regulatory citation %s for Entity %s in case %s",
                    regulatory_citation, entity_id, case_id)
    return citations

def get_documents(case_id, bucket, bucket_name):
    documents = []
    with db.engine.connect() as conn:
        rs = conn.execute(DOCUMENTS, case_id)
        for row in rs:
            document = {
                'document_id': row['document_id'],
                'category': row['category'],
                'description': row['description'],
                'length': row['length'],
                'text': row['ocrtext'],
                'document_date': row['document_date'],
            }
            pdf_key = 'legal/murs/current/%s.pdf' % row['document_id']
            logger.info("S3: Uploading {}".format(pdf_key))
            bucket.put_object(Key=pdf_key, Body=bytes(row['fileimage']),
                    ContentType='application/pdf', ACL='public-read')
            document['url'] = "https://%s.s3.amazonaws.com/%s" % (bucket_name, pdf_key)
            documents.append(document)
    return documents

def remove_reclassification_notes(statutory_citation):
    """ Statutory citations include notes on reclassification of the form
    "30120 (formerly 441d)" and "30120 (formerly 432(e)(1))". These need to be
    removed as we explicitly perform the necessary reclassifications.
    """
    UNPARENTHESIZED_FORMERLY_REGEX = re.compile(r' formerly \S*')
    PARENTHESIZED_FORMERLY_REGEX = re.compile(r'\(formerly ')

    def remove_to_matching_parens(citation):
        """ In the case of reclassification notes of the form "(formerly ...",
        remove all characters up to the matching closing ')' allowing for nested
        parentheses pairs.
        """
        match = PARENTHESIZED_FORMERLY_REGEX.search(citation)
        pos = match.end()
        paren_count = 0
        while pos < len(citation):
            if citation[pos] == ')':
                if paren_count == 0:
                    return citation[:match.start()] + citation[pos + 1:]
                else:
                    paren_count -= 1
            elif citation[pos] == '(':
                paren_count += 1
            pos += 1
        return citation[:match.start()]  # Degenerate case - no matching ')'

    cleaned_citation = UNPARENTHESIZED_FORMERLY_REGEX.sub(' ', statutory_citation)
    while PARENTHESIZED_FORMERLY_REGEX.search(cleaned_citation):
        cleaned_citation = remove_to_matching_parens(cleaned_citation)
    return cleaned_citation