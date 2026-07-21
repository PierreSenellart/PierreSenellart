#!/usr/bin/env ruby
# Print, as JSON, GitHub Linguist's per-language file breakdown for a repo:
#   {"Python": ["a.py", "b.py"], "C++": [...], ...}
#
# This is the *authoritative* file selection -- it honours each repo's
# .gitattributes (linguist-vendored / -documentation / -generated), drops
# binaries, vendored and generated content exactly like GitHub's /languages
# endpoint does. gen_top_langs.py counts the lines of these files to get an
# honest LOC total that matches the byte-based percentages.
#
# Usage: ruby linguist_breakdown.rb /path/to/cloned/repo
require 'linguist'
require 'rugged'
require 'json'

repo = Rugged::Repository.new(ARGV[0] || '.')
lr = Linguist::Repository.new(repo, repo.head.target_id)
puts JSON.generate(lr.breakdown_by_file)
