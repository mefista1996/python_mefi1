# BEGIN
def say_hurray_three_times():
    word = 'hurray!'
    return f'{word} {word} {word}'
# END

# my solution
def say_hurray_three_times():
    return 'hurray! hurray! hurray!'

____________________________________________

def get_last_char(hui):
    return hui[-1]


print(get_last_char('Hexlet'))
print(get_last_char('Vika'))
print(get_last_char('kostenko'))

#Можно ли записать в качестве аргумента какое-то значение в определении функции?
#Нельзя. Аргумент принимает значение при вызове функции, поэтому он должен быть переменной

num1 = input("Give me the number:")
num2 = input("Add the number you want to add")
result = float(num1) + float(num2)
_____________________________________________________

print(result)

def truncate(text , lenght):
    result = f"{text[0:lenght]}..."
    return result

print(truncate("текст", 3))


def get_age_difference(n1 , n2):
    result = n1 - n2
    print("The age difference is " + str(result))

get_age_difference(2011,2000)

def get_age_difference(year_one, year_two):
    difference = abs(year_one - year_two)
    return f"The age difference is {difference}"

______________________________________________________

def is_leap_year(year):
    return (year % 400 == 0) or year % 4 == 0 and not year % 100 == 0

print (is_leap_year(2016))


def even_number (number):
    return number % 2 == 0 and 'Yes' or 'No'

print(even_number(5))

______________________________________
def string_or_not(word):
    return isinstance(word, str) and 'yes' or 'no'

print(string_or_not('hello world'))

def normalize_url(url):
    https = 'https://'
    if url[:8] == https:
        return url
    elif url[:7] == 'http://':
        return https + url[7:]
    else:
        return https + url

    #LIST (RANDOM)
chosen =""
chosen_list=[]
available_list = ['Monday Morning', 'Monday Afternoon', 'Monday Evening', 'Tuesday Morning', 'Tuesday Afternoon', 'Tuesday Evening']
print ('What is your availability?')
while chosen !="0":
    print ('Available times:' + ",".join(available_list))
    chosen = input('Choose a time or 0 to quit')
    if chosen in available_list:
        chosen_list.append(chosen)
        print (chosen_list)

__________________________________________________________

#Checking range - task from book
        def cash_check(num):
            if num >= 100 and num <= 500:
                print("Yeah, this is what we need")
            elif num >= 1000 and num <= 5000:
                print("That's even more!")
            else:
                print("No, not within the range")


cash_check(1000)


def ninja_check(num):
    if num <= 50 and num > 30:
        print("Too much")
    elif num >=10 and num < 30:
        print("Might be not easy, but I got it")
    elif num < 10:
        print("I'll kick their asses")

found = 20
magic_coins = 70
stolen_coins = 3
coins = found
for week in range(1,53):
    coins = coins+magic_coins-stolen_coins
    print('Week %s = %d' % (week,coins))

def age_joke():
    age = input('How old are you?: ')
    age = int(age)
    if age <50:
        print('Damn, you are so young')
    else:
        print('I mean, what???')

age_joke()

number_grid = [
    [1,2,3],
    [4,5,6],
    [7,8,9],
    [0]
]

for row in number_grid:
    for column in row:
        print(column)


def translate (phrase):
    translation = ""
    for letter in phrase:
        if letter in "AEIOUaeiou":
            translation = translation + 'g'
        else:
            translation = translation + letter
    return translation

print(translate(input('Enter here:')))


import sys

# Read in an input string passed in to our script as an argument
input_string = sys.argv[1]
output_string = input_string


var1 = output_string
var2 = output_string.upper()
var3 =  var2 + '!!!'
print(var3)

import sys

# This code reads in arguments and converts those inputs to decimal numbers
first_number = float(sys.argv[1])
second_number = float(sys.argv[2])

# Your code goes here!
result_sum = first_number + second_number
result_difference = first_number - second_number
result_product = first_number*second_number
result_quotient = first_number/second_number
print(f"{first_number} plus {second_number} equals {result_sum}")
print(f"{first_number} minus {second_number} shows {result_difference}")
print(f"{first_number} multiplied by {second_number} equals {result_product}")
print(f"{first_number} divided by {second_number} equals {result_quotient}")


#print multiple strings with adding a space

string = "hello"
int = int(10)
space = " "

list = []

list.append(string*int)

for x in list:
    x = x[0:5]
    x = x + space
    list.clear()
    list.append(x*int)
    print(list)
    break
