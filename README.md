# UF_library_book_data_scraper
Scrapes the book data in the UF library using the Primo Library website which goal is to approximate the location of the book in a library. Data only have floor 3 marston


The main files to look at is:
- uf_callnumber_search.py obtain book date from search a call number used instead of title since shelved are ranged using call numbers 
- shelf_density.py repeatedly call the about script to get the data of entire bookshelf. Not recommend in long range broach search.

This gives a csv whic holds the data of all the scraped title founds
due to shelf_density.py taking so long a json cache is used to save progressed. 

Scraping took roughly 7-8 hours for the entire thirds floor of marston, only finished 30% of fourth floor so far due to thhe large amount of books.
shelves.csv is used to get the range of each shelves and determine the number of bins in the shelf 

Made with Claude
