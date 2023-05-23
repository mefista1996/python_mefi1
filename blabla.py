def my_substr(string, length):
    result_string = ''
    index = 0
    while length >= index:
        result_string = string[index]
        print(result_string)
        index += 1

my_substr('hello darkenss my old friend', 0)