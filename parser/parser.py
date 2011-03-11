#!/usr/bin/python

''' Parse the plain text version of congressional record documents and mark them up with xml.'''

import re, datetime, os, sys
import urllib2
from xml.sax.saxutils import escape, unescape
from settings import CWOD_HOME, LOG_DIR

import lxml.etree


DEBUG = False

class UnrecognizedCRDoc(Exception):
    pass

class AlignmentError(Exception):
    pass

class Regex(object):

    def __init__(self, string):
        self.string = string
        # a list of tuples containing (regex_string, xml_opening_tag)
        self.opentags = []
        self.closetags = []

    def insert_before(self, re_string, tag, group=None):
        # start tags are inserted at the start of a regex match. if group is
        # specified, matched at the beginning of the group instead. 
        self.opentags.append((re_string, tag, group))
    
    def insert_after(self, re_string, tag, group=None):
        # start tags are inserted at the start of a regex match. if group is
        # specified, matched at the end of the group instead. 
        self.closetags.append((re_string, tag, group))


    def apply(self):
        indexes = {}
        # identify where all the opening tags go (those that get inserted at
        # the start of the regex match)
        for regex, tag, group in self.opentags:
            matchobj = re.search(regex, self.string)
            if matchobj: 
                if group:
                    start = matchobj.start(group)
                else:
                    start = matchobj.start()
                # the tag for a given position is stored as a list, because
                # there may be more than one tag that goes here. (eg a quote
                # that end at the end of a paragraph).
                if start not in indexes:
                    indexes[start] = []
                indexes[start].append(tag)

        # identify where all the closing tags go (those that get inserted at
        # the end of the regex match)
        for regex, tag, group in self.closetags:
            matchobj = re.search(regex, self.string)
            if matchobj:
                if group:
                    end = matchobj.end(group)
                else:
                    end = matchobj.end()
                # the tag for a given position is stored as a list, because
                # there may be more than one tag that goes here. (eg a quote
                # that end at the end of a paragraph).
                if end not in indexes:
                    indexes[end] = []
                indexes[end].append(tag)

        if len(indexes):
            #print indexes

            # we need to split the string into substrings between each pair of
            # (sorted) indices, eg. at index_n and index_n+1. a substring is
            # also needed from the beginning of the string to the first split
            # index, and from the last split index to the end of the string.  
            l = indexes.keys()
            l.sort() 
            first_substring = [(0,l[0])] 
            last_substring = [(l[-1], len(self.string))]
            pairs = first_substring + [(l[i], l[i+1]) for i in xrange(len(l)-1)] + last_substring
	        
            output = []
	        # make sure we don't duplicate any insertions. 
            already_matched = []
            for start, stop in pairs:
                substr = self.string[start:stop]
                # is there a tag that goes here?
                if start in indexes.keys() and start not in already_matched:
                    output.append(substr)
                    for tag in indexes[start]:
                        output.append(tag)
                    already_matched.append(start)
                elif stop in indexes.keys() and stop not in already_matched:
                    output.append(substr)
                    for tag in indexes[stop]:
                        output.append(tag)
                    already_matched.append(stop)
                else:
	                output.append(substr)
	        # now join the pieces of the output string back together
	        outputstring = ''.join(output)
            return outputstring
        else:
            # if there were no matches, return the string unchanged.
            return self.string

class XMLAnnotator(object):
    def __init__(self, string):
        self.regx = Regex(string)

    def register_tag(self, re_string, open_tag, group=None):
        ''' Registers an XML tag to be inserted around a matching regular
        expression. The closing tag is derived from the opening tag. This
        function only registers the tags and their associated regex; apply()
        must be run before the tags are inserted. If group is specified, then
        the the tag is inserted around the matching group instead of the entire
        regular expression. ''' 

        close_tag = self.derive_close_tag(open_tag)
        self.regx.insert_before(re_string, open_tag, group)
        self.regx.insert_after(re_string, close_tag, group)

    def register_tag_open(self, re_string, open_tag, group=None):
        self.regx.insert_before(re_string, open_tag, group)

    def register_tag_close(self, re_string, close_tag, group=None):
        self.regx.insert_after(re_string, close_tag, group)

    def derive_close_tag(self, open_tag):
        space = open_tag.find(' ')
        if space != -1:
            close_tag = '</' + open_tag[1:space] + '>'
        else:
            close_tag = '</' + open_tag[1:]
        return close_tag

    def apply(self):
        return self.regx.apply()


class CRParser(object):
    ''' Parser functionality and regular expressions common to all
    congressional record documents'''

    re_volume =             r'(?<=Volume )\d+'
    re_number =             r'(?<=Number )\d+'
    re_weekday =            r'Number \d+ \((?P<weekday>[A-Za-z]+)'
    re_month =              r'\([A-Za-z]+, (?P<month>[a-zA-Z]+)'
    re_day =                r'\([A-Za-z]+, [A-Za-z]+ (?P<day>\d{1,2})'
    re_year =               r'\([A-Za-z]+, [A-Za-z]+ \d{1,2}, (?P<year>\d{4})'
    re_chamber =            r'(?<=\[)[A-Za-z]+'
    re_pages =              r'Pages? (?P<pages>[\w\-]+)'
    re_title_start =        r'\S+'
    re_title =              r'\s+(?P<title>(\S ?)+)'
    re_title_end =          r'.+'
    re_newpage =            r'\[\[Page \w+\]\]'
    re_timestamp =          r'{time}\s\d{4}'
    re_underscore =         r'\s+_+\s+'
    # a new speaker might either be a legislator's name, or a reference to the role of president of presiding officer. 
    re_newspeaker =         r'^(<bullet> |  )(?P<name>M(r|s)s?\. [A-Z]+|%s|((The ((ACTING|Acting) )?(PRESIDENT|SPEAKER|CHAIR(MAN)?)( pro tempore)?)|(The PRESIDING OFFICER)|(The CLERK)|(The VICE PRESIDENT))( \([A-Za-z.\- ]+\))?)\.'

    # whatever follows the statement of a new speaker marks someone starting to
    # speak. if it's a new paragraph and there's already a current_speaker,
    # then this re is also used to insert the <speaking> tag. 
    re_speaking =           r'^(<bullet> |  )((((((Mr)|(Ms)|(Mrs))\. [A-Za-z \-]+(of [A-Z][a-z]+)?)|((The (ACTING )?(PRESIDENT|SPEAKER)( pro tempore)?)|(The PRESIDING OFFICER)|(The CLERK))( \([A-Za-z.\- ]+\))?))\. )?(?P<start>.)'
    re_startshortquote =    r'``'
    re_endshortquote =      r"''"
    re_billheading =        r'\s+SEC.[A-Z_0-9. \-()\[\]]+'
    re_longquotestart =     r' {7}(?P<start>.)'
    re_longquotebody =      r' {5}(?P<start>.)' 
    re_endofline =          r'$'
    re_startofline =        r'^'
    re_alltext =            r"^\s+(?P<text>\S([\S ])+)"
    re_rollcall =           r'\[Roll(call)?( Vote)? No. \d+.*\]'
    re_allcaps =            r'^[A-Z][^a-z]*$'

    re_recorderstart =      (r'^\s+(?P<start>'
                             + r'(The (assistant )?legislative clerk read as follows)'
                             + r'|(The nomination considered and confirmed is as follows)'
                             + r'|(The (assistant )?legislative clerk)'
                             + r'|(The nomination was confirmed)'
                             + r'|(There being no objection, )'
                             + r'|(The resolution .*?was agreed to.)'
                             + r'|(The preamble was agreed to.)'
                             + r'|(The resolution .*?reads as follows)'
                             + r'|(The assistant editor .*?proceeded to call the roll)'
                             + r'|(The bill clerk proceeded to call the roll.)'
                             + r'|(The bill clerk called the roll.)'
                             + r'|(The motion was agreed to.)'
                             + r'|(The Clerk read the resolution, as follows:)'
                             + r'|(The resolution(, with its preamble,)? reads as follows:)'
                             + r'|(The amend(ment|ed).*?(is)? as follows:)'
                             + r'|(Amendment No\. \d+.*?is as follows:)'
                             + r'|(The yeas and nays resulted.*?, as follows:)'
                             + r'|(The result was announced.*?, as follows:)'
                             + r'|(The .*?editor of the Daily Digest)'
                             + r'|(The (assistant )?bill clerk read as follows:)'
                             + r'|(The .*?read as follows:)'
                             + r'|(The text of the.*?is as follows)'
                             + r'|(amended( to read)? as follows:)'
                             + r'|(The material (previously )?referred to (by.*?)?is as follows:)'
                             + r'|(There was no objection)'
                             + r'|(The amendment.*?was agreed to)'
                             + r'|(The motion to table was .*)'
                             + r'|(The question was taken(;|.))'
                             + r'|(The following bills and joint resolutions were introduced.*)'
                             #+ r'|()'
                            + r').*')

    # anchored at the end of the line
    re_recorderend =        (r'('
                            + r'(read as follows:)'
                            + r'|(the Record, as follows:)'
                            + r'|(ordered to lie on the table; as follows:)'
                            + r'|(resolutions as follows:)'
                            + r')$')


    # sometimes the recorder says something that is not unique to them but
    # which, in the right context, we take to indicate a recorder comment. 
    re_recorder_fuzzy =     (r'^\s+(?P<start>'
                             + r'(Pending:)'
                             #+ r'|()'
                            + r').*')


    LINE_MAX_LENGTH =           71
    LONGQUOTE_INDENT =          5
    NEW_PARA_INDENT =           2
    LONGQUOTE_NEW_PARA_INDENT = [6,7]

    # documents with special titles need to be parsed differently than the
    # topic documents, either because they follow a different format or because
    # we derive special information from them. in many cases these special
    # titles are matched as prefixes, not just full text match. 
    special_titles = {
        "senate" : "" ,
        "Senate" : "" ,
        "prayer" : "",
        "PLEDGE OF ALLEGIANCE" : "",
        "APPOINTMENT OF ACTING PRESIDENT PRO TEMPORE" : "",
        "RECOGNITION OF THE MAJORITY LEADER" : "",
		"SCHEDULE" : "",
        "RESERVATION OF LEADER TIME" : "",
        "MORNING BUSINESS" : "",
        "MESSAGE FROM THE HOUSE" : "",
        "MESSAGES FROM THE HOUSE" : "",
        "MEASURES REFERRED" : "",
        "EXECUTIVE AND OTHER COMMUNICATIONS" : "",
        "SUBMITTED RESOLUTIONS" : "",
        "SENATE RESOLUTION" : "", 
		"SUBMISSION OF CONCURRENT AND SENATE RESOLUTIONS" : "",
		"ADDITIONAL COSPONSORS" : "",
		"ADDITIONAL STATEMENTS" : "",
		"REPORTS OF COMMITTEES" : "", 
		"INTRODUCTION OF BILLS AND JOINT RESOLUTIONS" : "",
		"ADDITIONAL COSPONSORS" : "", 
        "INTRODUCTION OF BILLS AND JOINT RESOLUTIONS" : "",
		"STATEMENTS ON INTRODUCED BILLS AND JOINT RESOLUTIONS" : "", 
		"AUTHORITY FOR COMMITTEES TO MEET" : "", 
		"DISCHARGED NOMINATION" : "", 
		"CONFIRMATIONS" : "", 
		"AMENDMENTS SUBMITTED AND PROPOSED" : "",
		"TEXT OF AMENDMENTS" : "",
		"MEASURES PLACED ON THE CALENDAR" : "",
		"EXECUTIVE CALENDAR" : "",
        "REPORTS OF COMMITTEES DURING ADJOURNMENT" : "",
        "MEASURES DISCHARGED" : "",
        "REPORTS OF COMMITTEES ON PUBLIC BILLS AND RESOLUTIONS": "",
        "INTRODUCTION OF BILLS AND JOINT RESOLUTIONS": "",
    }

    def __init__(self, abspath):
        # track error conditions
        self.error_flag = False

        # file data
        self.filename = abspath
        print self.filename
        fp = open(abspath)

        self.rawlines = []
        newpage = False

        self.date = None

        # state information
        self.currentline = 0
        self.current_speaker = None
        self.inquote = False
        self.intitle = False
        self.new_paragraph = False
        self.recorder = False
        self.inlongquote = False
        self.newspeaker = False
        self.inrollcall = False
        self.is_bullet = False

        # Remove page numbers and timestamps.
        pages = fp.read()
        pages = re.sub(r'\n\n\[\[Page.*?\]\]\n\n', ' ', pages)
        pages = re.sub(r'\n\n +\{time\} +\d+\n', '', pages)
        if re.search(r'<bullet>', pages):
            self.is_bullet = True
            pages = re.sub(r'<bullet>', ' ', pages)
        self.rawlines = pages.split('\n')

        # output
        self.xml = ['<CRDoc>', ]


    def spaces_indented(self, theline):
        ''' returns the number of spaces by which the line is indented. '''
        re_textstart = r'\S'
        m = re.search(re_textstart, theline)
        if m:
            return m.start()
        return 0
        #return re.search(re_textstart, theline).start()
        
    def parse(self):
        ''' parses a raw senate document and returns the same document marked
        up with XML '''
        self.get_metadata()
        self.markup_preamble()

    def download_mods_file(self):
        path = os.path.split(self.filename)[0]
        pieces = path.split('/')
        year, month, day = pieces[-3:]
        url = 'http://www.gpo.gov/fdsys/pkg/CREC-%(year)s-%(month)s-%(day)s/mods.xml' % locals()
        print 'No mods file found locally. Downloading from %s' % url
        page = urllib2.urlopen(url).read()
        fh = open(os.path.join(path, 'mods.xml'), 'w')
        fh.write(page)
        fh.close()
        self.get_metadata()

    def get_metadata(self):
        path, filename = os.path.split(self.filename)
        xml_filename = os.path.join(path, 'mods.xml')
        granule = filename.split('.')[0]

        try:
            xml = open(xml_filename, 'r').read()
        except IOError:
            self.download_mods_file()
        
        # Remove namespace to make using xpath easier.
        xml = xml.replace('xmlns="http://www.loc.gov/mods/v3" ', '')
        doc = lxml.etree.fromstring(xml)
        self.volume = doc.xpath('extension/volume')[0].text
        self.issue = doc.xpath('extension/issue')[0].text
        self.congress = doc.xpath('extension/congress')[0].text
        self.session = doc.xpath('//session')[0].text
        try:
            item = doc.xpath('//relatedItem[@ID="id-%s"]' % granule)[0]
        except IndexError:
            print 'Item not found in xml: %s' % granule
            sys.exit(1)

        # Get the document title
        self.document_title = escape(item.xpath('titleInfo/title')[0].text)

        # Get the names of the members of Congress listed
        self.members = []
        for member in item.xpath('extension/congMember'):
            data = member.attrib
            data.update({'name': member.xpath('name')[0].text, })
            self.members.append(data)
        self.re_newspeaker = self.re_newspeaker % '|'.join([x['name'] for x in self.members])

        self.referenced_by = []
        for related_item in item.xpath('relatedItem'):
            if related_item.attrib.get('type') == 'isReferencedBy':
                for identifier in related_item.xpath('identifier'):
                    data = identifier.attrib
                    data.update({'text': identifier.text, })
                    #print data
                    self.referenced_by.append(data)

    def markup_preamble(self):
        self.currentline = 1
        theline = self.rawlines[self.currentline]
        annotator = XMLAnnotator(theline)
        annotator.register_tag(self.re_volume, '<volume>')
        annotator.register_tag(self.re_number, '<number>')
        annotator.register_tag(self.re_weekday, '<weekday>', group='weekday')
        annotator.register_tag(self.re_month, '<month>', group='month')
        annotator.register_tag(self.re_day, '<day>', group='day')
        annotator.register_tag(self.re_year, '<year>', group='year')
        xml_line = annotator.apply()
        #print xml_line
        self.xml.append(xml_line)
        if self.is_bullet:
            self.xml.append('<bullet>1</bullet>')
        self.markup_chamber()

    def markup_chamber(self):
        self.currentline = 2
        theline = self.rawlines[self.currentline]
        annotator = XMLAnnotator(theline)
        annotator.register_tag(self.re_chamber, '<chamber>')
        xml_line = annotator.apply()
        #print xml_line
        self.xml.append(xml_line)
        self.markup_pages()    
        
    def markup_pages(self):
        self.currentline = 3
        theline = self.rawlines[self.currentline]
        annotator = XMLAnnotator(theline)
        annotator.register_tag(self.re_pages, '<pages>', group='pages')
        xml_line = annotator.apply()
        #print xml_line
        self.xml.append(xml_line)
        self.xml.append('<congress>%s</congress>' % self.congress)
        self.xml.append('<session>%s</session>' % self.session)
        self.markup_title()

    def clean_line(self, theline):
        ''' strip unwanted parts of documents-- page transitions and spacers.'''
        newpage = re.match(self.re_newpage, theline)
        if newpage:
            theline = theline[:newpage.start()]+theline[newpage.end():]
        underscore = re.match(self.re_underscore, theline)
        if underscore:
            theline = theline[:underscore.start()]+theline[underscore.end():]
        # note: dont strip whitespace when cleaning the lines because
        # whitespace is often the only indicator of the line's purpose or
        # function. 
        return escape(theline)

    def get_line(self, offset=0):
        if self.currentline+offset > len(self.rawlines)-1:
            return None
        return self.clean_line(self.rawlines[self.currentline+offset])

    def is_special_title(self, title):
        title = title.strip()
        special_title_prefixes = self.special_titles.keys()
        for prefix in special_title_prefixes:
            if re.search(prefix, title):
                return True
        return False

    def markup_title(self):
        ''' identify and markup the document title. the title is some lines of
        text, usually but not always capitalized, usually but not always
        centered, and followed by a least one empty line. they sometimes have a
        line of dashes separating them from the body of the document. and
        sometimes they don't exist at all.'''

        MIN_TITLE_INDENT = 4

        # skip line 4; it contains a static reference to the GPO website.  
        self.currentline = 5
        theline = self.get_line()
        while not theline.strip():
            self.currentline += 1
            theline = self.get_line()
        
        # we're going to check what kind of title this is once we're done
        # parsing it, so keep track of where it starts. since all the special
        # titles are uniquely specified by their first line, we only need to
        # track that. 
        title_startline = theline

        # if it's not a specially formatted title and it's not indented enough,
        # then it's probably missing a title altogether
        if self.spaces_indented(theline) < MIN_TITLE_INDENT and not self.is_special_title(self.document_title) and theline.strip() != self.document_title:
            self.markup_paragraph()

        else:
            if self.is_special_title(self.document_title):
                self.currentline +=1
                theline = self.get_line()
                self.xml.append('<document_title>%s</document_title>\n' % self.document_title)
                self.markup_paragraph()
            else:
                # regular title
                #annotator = XMLAnnotator(theline)
                #annotator.register_tag_open(self.re_title_start, '<document_title>')
                self.currentline +=1
                theline = self.get_line()
                self.xml.append('<document_title>%s</document_title>\n' % self.document_title)

                # note that as we exit this function, the current line is one PAST
                # the end of the title, which should generally be a blank line. 
                self.markup_paragraph()


    def set_speaker(self, theline):
        # checks if there is a new speaker, and if so, set the current_speaker
        # attribute, and returns the name of the new (and now current) speaker.
        # else leaves the current speaker.  
        new_speaker = re.search(self.re_newspeaker, theline)
        if new_speaker:
            name = new_speaker.group('name')
            self.current_speaker = name
        return self.current_speaker

    def check_bullet(self, theline):
        if unescape(theline).find('<bullet>') >= 0:
            self.is_bullet = True
            self.rawlines[self.currentline] = self.rawlines[self.currentline].replace('<bullet>', ' ')
            # now start at the end of the document and walk up the doc, to find
            # the closing bullet tag. 
            ix = len(self.rawlines)-1
            while True:
                if self.rawlines[ix].find('<bullet>') >= 0:
                    self.rawlines[ix] = self.rawlines[ix].replace('<bullet>', '')
                    return self.rawlines[self.currentline]
                ix -= 1
        else:
            return theline

    def markup_paragraph(self):
        ''' this is the standard paragraph parser. handles new speakers,
        standard recorder comments, long and short quotes, etc. '''
        
        # get to the first line 
        theline = self.get_line()
        while not theline.strip():
            self.currentline += 1
            theline = self.get_line()

        if self.is_title(theline):
            annotator = XMLAnnotator(theline)
            annotator.register_tag_open(self.re_title, '<title>', group='title')
            while(self.is_title(theline)):
                self.currentline += 1
                theline = self.get_line()
            annotator.register_tag_close(self.re_title_end, '</title>')
            xml_line = annotator.apply()
            self.xml.append(xml_line)
            self.currentline +=1

        theline = self.get_line()
        while not theline.strip():
            self.currentline += 1
            theline = self.get_line()


        self.document_first_line = True

        while theline:
            if list(set(theline.strip())) == ['-',] or list(set(theline.strip())) == ['_'] or len(theline.strip()) == 0:
                self.currentline +=1
                theline = self.get_line()
                continue
            self.preprocess_state(theline)
            annotator = XMLAnnotator(theline)
            if self.intitle:
                annotator.register_tag(self.re_title, '<title>', group='title')
            # some things only appear on the first line of a paragraph
            elif self.inrollcall:
                # will only match on first line of the roll call
                annotator.register_tag_open(self.re_rollcall, '<rollcall>')
            elif self.new_paragraph:
                annotator.register_tag_open(self.re_longquotestart, '<speaking name="%s" quote="true">' % (self.current_speaker), group='start')
                if self.recorder:
                    annotator.register_tag_open(self.re_startofline, '<recorder>')
                annotator.register_tag(self.re_newspeaker, '<speaker name="%s">' % self.current_speaker, group='name')
                if self.return_from_quote_interjection(theline):
                    annotator.register_tag_open(self.re_longquotebody, '<speaker name="%s" quote="true">' % (self.current_speaker), group='start')
                if not self.recorder and not self.inlongquote:
                    # check the current speaker-- if it's the recorder, then
                    # even though this isn't a "known" recorder sentence,
                    # there's no other speaker so we treat it like a recorder
                    # comment.
                    if self.current_speaker == 'recorder':
                        annotator.register_tag_open(self.re_speaking, '<recorder>', group='start')
                        self.recorder=True
                    else: 
                        annotator.register_tag_open(self.re_speaking, '<speaking name="%s">' % (self.current_speaker), group='start')

            if not self.intitle and not self.inlongquote and not self.inrollcall:
                pass
            #    annotator.register_tag_open(self.re_startshortquote, '<quote speaker="%s">' % self.current_speaker)

            # note: the endquote tag needs to be registered BEFORE the end
            # speaking tag, because the quote tag should appear before (be
            # nested within) the speaking tag. a nesting functionality should
            # really be implemented within the XMLAnnotator class, but this
            # will do for now. 
            if not self.inlongquote and not self.intitle and not self.inrollcall:
                if self.inquote:
                    #annotator.register_tag_close(self.re_endshortquote, '</quote>')
                    #annotator.register_tag_close(self.re_endshortquote, '</speaking>')
                    pass

            if self.paragraph_ends():
                if self.inrollcall: 
                    annotator.register_tag_close(self.re_endofline, '</rollcall>')
                    self.inrollcall = False
                elif self.recorder:
                    annotator.register_tag_close(self.re_endofline, '</recorder>')
                elif self.inlongquote:
                    if self.longquote_ends():
                        #print 'i think the longquote ends'
                        #annotator.register_tag_close(self.re_endofline, '</quote>')
                        annotator.register_tag_close(self.re_endofline, '</speaking>')
                elif self.intitle:
                    pass
                #  this specific set of states usually means we're somewhere
                #  unrecognized, and can without these caveats can end up with
                #  stray </speaking> tags. 
                elif (self.current_speaker == 'recorder' and self.inlongquote == False and self.inrollcall == False
                    and self.recorder == False and self.inquote == False and self.intitle == False):
                    #print "UNRECOGNIZED STATE (but that's ok): %s" % theline
                    pass
                elif self.current_speaker:
                    annotator.register_tag_close(self.re_endofline, '</speaking>')

            #if (self.current_speaker == 'recorder' and self.inlongquote == False and self.inrollcall == False
            #    and self.recorder == False and self.inquote == False and self.intitle == False):
            #    print "UNRECOGNIZED STATE (but that's ok): %s" % theline
            #    annotator.register_tag(self.re_alltext, '<unknown>', group='text')
 
            xml_line = annotator.apply()
            #print xml_line
            self.xml.append(xml_line)

            # do some post processing
            self.postprocess_state(theline)

            # get the next line and do it all again
            self.currentline +=1
            theline = self.get_line()
            while theline is not None and not theline.strip():
                self.currentline += 1
                #print 'currentline: %d' % self.currentline
                theline = self.get_line()

            if not theline:
            #if theline is None:
                # end of file
                self.xml.append('</CRDoc>')

        if self.xml[-1] != '</CRDoc>':
            self.xml.append('</CRDoc>')

    def matching_tags(self, open, close):
        ''' determine if the close tag matches the open tag '''
        space = open.find(' ')
        if space != -1:
            derived_close = '</' + open[1:space] + '>'
        else:
            derived_close = '</' + open[1:]
        if derived_close == close:
            return True
        else:
            return False


    def validate(self):
        ''' validate the xml in the file, checking for mismatched tags and
        removing any tags if necessary. basically, it's more important for the
        document to validate than to get everything perfect.'''
        
        re_opentag = r'<[A-Za-z_]+( [a-z]+=".*?")?>'
        re_closetag = r'</[A-Za-z_]+>'
        re_tag = '</?.+?>'

        active = []
        orphans = []
        for linenum, line in enumerate(self.xml):
            tagiter = re.finditer(re_tag, line)
            tags = [(match.group(), match.start(), match.end(), linenum) for match in tagiter]
            for taginfo in tags:
                tagname = taginfo[0]
                if re.search(re_opentag, tagname):
                    active.append(taginfo)
                    #print active
                elif re.search(re_closetag, tagname):
                    #print 'line: %s' % self.xml[taginfo[3]].strip('\n')
                    #print 'comparing %s and %s' % (active[-1][0], tagname)
                    if len(active) and self.matching_tags(active[-1][0], tagname):
                        del active[-1]
                    else:
                        print 'no match-- orphaned\n'
                        orphans.append(taginfo)
        # append any remaining, unclosed open tags to the orphan list
        if len(orphans):
            logfile = initialize_logfile()
            logfile.write('%s : orphaned tags\n' % self.filename)
        orphans.extend(active)
        # BUT, we don't want to remove the CRDoc tags
        save = []
        for orphan in orphans:
            if orphan[0] == '<CRDoc>' or orphan[0] == '</CRDoc>':
                #print 'saving crdoc tag', orphan[0]
                save.append(orphan)
        for s in save:
            orphans.remove(s)

        """
        print 'Before Validation:\n'
        print ''.join(self.xml)
        print self.filename
        print '\n\n'
        """

        """
        print 'Orphaned Tags:\n'
        for orphan in orphans:
            print orphan, self.xml[orphan[3]]
        """
        
        for orphan in orphans:
            linenum = orphan[3]
            theline = self.xml[linenum]
            # we have to use start and end indices instead of replace, since
            # replace will replace *all* occurences
            start = orphan[1]
            end = orphan[2]
            self.xml[linenum] = theline[:start]+theline[end:]

        """
        print '\nAfter Validation:\n'
        print ''.join(self.xml)
        print self.filename
        print '\n\n'
        print orphans
        """
        return

    def longquote_ends(self):
        # XXX this function is totally repeating patterns used in other
        # places... 

        offset = 1
        theline = self.get_line(offset)

        while theline and not theline.strip():
            offset += 1
            theline = self.get_line(offset)
    
        # there should only be NO line if it's the end of the document
        if not theline:
            return True
        # longquote ends when the new paragraph is NOT another longquote
        # paragraph (be it a new title, vote, or just regular paragraph). 
        if self.spaces_indented(theline) not in self.LONGQUOTE_NEW_PARA_INDENT:
            return True
        return False

    def preprocess_state(self, theline):
        ''' in certain cases we need to match a regular expression AND a state,
        so do some analysis to determine which tags to register. '''

        return_from_interjection = self.return_from_quote_interjection(theline)

        if self.is_new_paragraph(theline) or return_from_interjection:
            self.new_paragraph = True
            self.intitle = False

            # if there's a new speaker, we don't want to 
            #if re.search(self.re_newspeaker, theline):
            #    self.newspeaker = True
        
            # in the case of a long quote, we don't change the current speaker. 
            if re.search(self.re_longquotestart, theline) or return_from_interjection:
                # if it's a long quote but we're already IN a long quote, then
                # we don't want to mark the beginning again, so suppress the
                # new paragraph state. 
                if self.inlongquote == True:
                    self.new_paragraph = False
                self.inlongquote = True
            else: 
                self.inlongquote = False
                # if it's a recorder reading, then make a note.  
                # re_recroder_fuzzy looks for terms that indicate a
                # continuation of a recorder comment only if the recorder was
                # already speaking, but not otherwise. 
                if re.search(self.re_recorderstart, theline) or (self.current_speaker == 'recorder'
                    and re.search(self.re_recorder_fuzzy, theline)):
                    self.recorder = True
                    self.current_speaker = 'recorder' 
                else:
                    self.set_speaker(theline)
                    if self.current_speaker == None and self.document_first_line:
                        self.document_first_line = False
                        self.recorder = True
                        self.current_speaker = 'recorder' 

        elif re.search(self.re_rollcall, theline):
            self.inrollcall=True
            self.intitle = False
            self.new_paragraph = False

        elif not self.inlongquote and not self.inrollcall and self.is_title(theline):
            self.intitle = True
            self.new_paragraph = False

        elif re.search(self.re_billheading, theline):
            self.intitle = True
            self.inlongquote = False
            self.new_paragraph = False

        else:
            self.new_paragraph = False
            self.intitle = False

        # if a quote starts we are "in a quote" but we stay in that quote until
        # we detect it ends. 
        if not self.inlongquote and re.search(self.re_startshortquote, theline):
            self.inquote = True

        # debugging..
        """
        print 'in title? %s' % self.intitle
        print 'new paragraph? %s' % self.new_paragraph
        if self.current_speaker:
            print 'current speaker: ' + self.current_speaker 
        else:
            print 'no current speaker'
        print 'in long quote? %s' % self.inlongquote
        print 'in recorder? %s' % self.recorder
        print 'in quote? %s' % self.inquote
        print 'in roll call? %s' % self.inrollcall
        """

    def postprocess_state(self, theline):
        ''' in certain cases where a state ends on a line, we only want to note
        that after the proper tags have been registered and inserted. ''' 
        
        # if we're in a long quote, the only way that we know the long quote is
        # over is when a new paragraph starts and is NOT a long quote. else,
        # just move along... nothing to see here. 
        if self.inlongquote:
            return

        if (not self.recorder and not self.inlongquote 
            and not self.intitle and not self.current_speaker):
            # this is a wierd state we shouldn't be in
            #print ''.join(self.rawlines)
            objdata = self.__dict__
            #print objdata['xml']
            del objdata['xml']
            del objdata['rawlines']
            #print ''
            #print objdata
            #print ''
            message = 'Unrecognized state while parsing %s.\n' % self.filename
            self.error_flag = True
            logfile = initialize_logfile()
            logfile.write('%s: Unrecognized state\n' % self.filename)
            logfile.flush()
            raise UnrecognizedCRDoc(message)

        # if there's one or more complete quotes (start and end) on a line, or
        # if a single quote ends that started on a previous line,  then we're
        # good to go and close the state. but if there's a quote that opens,
        # that doesn't close, we need to stay in this state.
        if self.inquote and re.search(self.re_endshortquote, theline):
            last_open_quote = theline.rfind("``")
            last_close_quote = theline.rfind("''")
            if last_open_quote == -1 or last_close_quote > last_open_quote:
                self.inquote = False

        # note that here we set self.recorder to be False whilst leaving
        # self.current_speaker set to 'recorder' (which it gets set to when a
        # recorder state is recognized). this half-state is used when parsing
        # long bits of verbatim material included in the CR as ready by the
        # recorder. 
        if self.recorder and self.paragraph_ends():
            self.recorder = False

        if self.intitle:
            self.intitle = False

    def return_from_quote_interjection(self, theline):
        ''' sometimes a paragraph in a long quote is not indented because it
        was only briefly interrupted for the reader to make a comment. but we
        still need to treat it like a new paragraph. '''

        if not escape(self.rawlines[self.currentline]) == theline:
            message = 'current line and index are not aligned'
            self.error_flag = True
            raise AlignmentError(message)

        line_above = self.rawlines[self.currentline -1].strip()
        two_lines_above = self.rawlines[self.currentline -2].strip()
        empty = ""

        if (self.spaces_indented(theline) == self.LONGQUOTE_INDENT and 
            line_above == empty and two_lines_above.endswith('--')):
            return True
        else:
            return False

    def paragraph_ends(self):
        ''' check if the current paragraph ends by looking ahead to what the
        next non-empty line is. idempotent. '''

        # a paragraph ending is really only indicated by the formatting which
        # follows it. if a line is followed by a new paragraph, a long section
        # of quoted text, or a subheading, then we know this must be the end of
        # athe current paragraph. almost all of those possibilities are
        # indicated by the indentation level.  
        offset = 1
        theline = self.get_line(offset)
        while theline and not theline.strip():
            offset += 1
            theline = self.get_line(offset)
    
        # if the document ends here, then it's certainly also the end of the
        # paragraph
        if not theline:
            return True
        if self.inrollcall:
            if self.spaces_indented(theline) == self.NEW_PARA_INDENT:
                return True
            else:
                return False
        # new para or new long quote?
        if self.is_new_paragraph(theline):
            return True
        # if the next line is a title then this paragraph is also over.
        if self.is_title(theline, offset=offset):
            return True
        # this strange case arises sometimes when legislators interject a
        # comment into the middle of something they are quoting/reading. 
        local_offset = self.currentline+offset
        line_above = self.rawlines[local_offset - 1].strip()
        first_line_on_page = re.search(self.re_newpage, self.rawlines[local_offset - 2])
        empty = ""
        if self.spaces_indented(theline) == self.LONGQUOTE_INDENT and line_above == empty and not first_line_on_page:
            return True
        # finally, if none of these cases are true, return false. 
        return False

    def is_centered(self, theline):
        if not theline.strip():
            return False
        left_align = re.search('\S', theline).start()
        right_align = (self.LINE_MAX_LENGTH - len(theline.strip()))/2
        # if the left and right align are the same (modulo off-by-one for
        # even-length titles) then we consider it centered, and therefore a
        # title. 
        if left_align in [right_align-1, right_align, right_align+1]:
            return True
        else:
            return False

    def is_title(self, theline, offset=0):
        #self.current_line +offset must be the index for theline
        local_offset = self.currentline + offset
        if not escape(self.rawlines[local_offset]) == theline:
            message = 'current line and index are not aligned'
            self.error_flag = True
            raise AlignmentError(message)

        first_line_on_page = re.search(self.re_newpage, self.rawlines[local_offset - 2])
        line_above = self.rawlines[local_offset - 1].strip('\n')
        line_below = self.rawlines[local_offset + 1].strip('\n')
        empty = lambda line: len(line.strip()) == 0

        # Without this, sometimes a newspeaker line 
        # will be mistaken as part of a title.
        if re.search(self.re_newspeaker, theline):
            return False

        if re.search(self.re_allcaps, theline):
            return True
        if self.is_centered(theline) and self.spaces_indented(theline) > 0:
            if (empty(line_above) and self.is_centered(line_below) and not re.search(self.re_recorderstart, theline)):
                return True
            if (empty(line_below) and self.is_centered(line_above)):
                if self.inlongquote:
                    return False
                else:
                    return True
            if (self.is_centered(line_above) and self.is_centered(line_below)):
                if self.inlongquote:
                    return False
                else:
                    return True
            if (empty(line_above) and empty(line_below)):
                # the first line on a page can look like a title because
                # there's an empty line separating new page designators from
                # page content. but, we know exactly what those look like so
                # eliminate that possibility here. 
                if not first_line_on_page:
                    return True
                elif self.spaces_indented(theline) > 2:
                    return True
        # this basically accounts for letter headers. note that the line
        # lengths include a character for the \n newline character. 
        if (empty(line_above) and 
            (empty(line_below) or self.spaces_indented(line_below) in self.LONGQUOTE_NEW_PARA_INDENT 
             or self.spaces_indented(line_below) == self.LONGQUOTE_INDENT) and 
            (len(theline) == 67 or len(theline) == 66 or len(theline) == 63)):
            return True
        # bill headers eg like  SEC. _03. SENSE OF CONGRESS.
        if re.search(self.re_billheading, theline):
            return True

        return False


    def is_new_paragraph(self, theline):
        if theline.startswith('<bullet>'):
            return True 
        if self.spaces_indented(theline) in self.LONGQUOTE_NEW_PARA_INDENT:
            return True
        if self.spaces_indented(theline) == self.NEW_PARA_INDENT:
            return True
        return False

    def save(self):
        ''' save the xml file to disk.'''
        saveas = self.filename.replace('raw', 'xml').replace('.txt', '.xml')
        savedir = os.path.dirname(saveas)
        if not os.path.exists(savedir):
            os.makedirs(savedir)
        fp = open(saveas, 'w')
        fp.write('\n'.join(self.xml))
        fp.close()
        print "saved file %s to disk" % saveas    

def usage():
    print ''
    print 'Usage:'
    print 'You must pass in a congressional record filename, or specify yyyy/mm/dd. eg:'
    print ''
    print './parser.py CREC-2010-07-12-pt1-PgS5744-2.txt'
    print './parser.py 2010/07/02 [interactive]'
    print ''
    print "The optional 'interactive' mode is for debugging and will prompt" 
    print "the user if they want to continue after each file."
    print ''
    sys.exit()

def initialize_logfile():
    ''' returns a filelike object'''
    if not os.path.exists(os.path.join(CWOD_HOME, LOG_DIR)):
        os.mkdir(os.path.join(CWOD_HOME, LOG_DIR))
    logfile = open(os.path.join(CWOD_HOME, LOG_DIR, 'parser.log'), 'a')
    return logfile

def parse_directory(path, interactive=False):
    logfile = initialize_logfile()
    for file in os.listdir(path):
        # we don't process the daily digest or front matter. 
        if file.find('FrontMatter') != -1 or file.find('PgD') != -1:
            continue
        if not file.endswith('.txt'):
            continue
        if interactive:
            resp = raw_input("process file %s? (y/n/q) " % file)
            if resp == 'n': 
                print 'skipping\n'
                continue
            elif resp == 'q':
                sys.exit()
        
        abspath = os.path.join(path, file)
        parser = CRParser(abspath)
        try:
            parser.parse()
            print 'flag status:', parser.error_flag
            if not parser.error_flag:
                parser.validate()
                parser.save()
        except Exception, e:
            today = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            logfile.write('%s: Error processing file %s\n' % (today, abspath))
            logfile.write('\t%s' % e)
            logfile.flush()

    output_dir = path.replace('raw', 'xml')
    return output_dir

if __name__ == '__main__':

    # processes a file or entire directory
    
    if len(sys.argv) < 2:
        usage()

    # if a single file is passed in, then determine the path (does NOT log,
    # just for debugging)
    if sys.argv[1].endswith('.txt'):
        file = sys.argv[1]
        if file.startswith('/'):
            print 'opening file %s' % file
            abspath = file
        else:
            # get date from filename
            parts = file.split('-')
            year = parts[1]
            month = parts[2]
            day = parts[3]
            abspath = os.path.join(CWOD_HOME, 'raw', '%s/%s/%s/%s' % (year, month, day, file))
            print 'processing file %s' % abspath
        parser = CRParser(abspath)
        parser.parse()
        if not parser.error_flag:
            parser.validate()
            parser.save()

    # if a date is passed in, process all files from that date
    else:
        date_path = sys.argv[1]
        path = os.path.join(CWOD_HOME, 'raw', date_path)
        print path
        if len(sys.argv) == 3 and sys.argv[2] == 'interactive':
            interactive = True
        else: interactive = False

        if not os.path.exists(path):
            print 'no records exist for that date. try a different date.'
            usage()

        parse_directory(path, interactive)
           
