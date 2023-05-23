%s - String (or any object with a string representation, like numbers)

%d - Integers

%f - Floating point numbers

%.<number of digits>f - Floating point numbers with a fixed amount of digits to the right of the dot.

%x/%X - Integers in hex representation (lowercase/uppercase)

< — меньше
<= — меньше или равно
> — больше
>= — больше или равно
== — равно
!= — не равно

#for cycles
a = a + 1 → a += 1
a = a - 1 → a -= 1
a = a * 2 → a *= 2
a = a / 1 → a /= 1

Тернарный оператор
Посмотрите на определение функции, которая возвращает модуль переданного числа:

def abs(number):
    if number >= 0:
        return number
    return -number
Но можно записать более лаконично. Для этого справа от return должно быть выражение, но if — это инструкция, а не выражение. В Python есть конструкция, которая работает как if-else, но считается выражением. Она называется тернарный оператор — единственный оператор в Python, который требует три операнда:

def abs(number):
    return number if number >= 0 else -number
Общий паттерн выглядит так: <expression on true> if <predicate> else <expression on false>.

def get_type_of_sentence(sentence):
    last_char = sentence[-1]
    return 'question' if last_char == '?' else 'normal'

print(get_type_of_sentence('Hodor'))   # => normal
print(get_type_of_sentence('Hodor?'))  # => question

_______________________________________________________

#інтерполяція

first_name = 'Joffrey'
greeting = 'Hello'

print(f'{greeting}, {first_name}!')

#агрегація данних
def sum_numbers_from_range(start, finish):
    i = start
    sum = 0  # Инициализация суммы
    while i <= finish:  # Двигаемся до конца диапазона
        sum = sum + i   # Считаем сумму для каждого числа
        i = i + 1       # Переходим к следующему числу в диапазоне
    # Возвращаем получившийся результат
    return sum

print(sum_numbers_from_range(5,7))

s = "Strings are awesome!"
# Length should be 20
print("Length of s = %d" % len(s))

# First occurrence of "a" should be at index 8
print("The first occurrence of the letter a = %d" % s.index("a"))

# Number of a's should be 2
print("a occurs %d times" % s.count("a"))

# Slicing the string into bits
print("The first five characters are '%s'" % s[:5]) # Start to 5
print("The next five characters are '%s'" % s[5:10]) # 5 to 10
print("The thirteenth character is '%s'" % s[12]) # Just number 12
print("The characters with odd index are '%s'" %s[1::2]) #(0-based indexing)
print("The last five characters are '%s'" % s[-5:]) # 5th-from-last to end

# Convert everything to uppercase
print("String in uppercase: %s" % s.upper())

# Convert everything to lowercase
print("String in lowercase: %s" % s.lower())

# Check how a string starts
if s.startswith("Str"):
    print("String starts with 'Str'. Good!")

# Check how a string ends
if s.endswith("ome!"):
    print("String ends with 'ome!'. Good!")

# Split the string into three separate strings,
# each containing only a word
print("Split the words of the string: %s" % s.split(" "))